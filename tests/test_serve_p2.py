"""The studio endpoints of `reincarnate serve`: shapes, validation, security."""

import threading
import time

import pytest

from p2_catalog import seed
from reincarnate import auth, design, paths, serve
from reincarnate.store import Store
from test_serve import TOKEN, Client


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.setattr(auth, "_probe", lambda: {"logged_in": True, "method": "claude.ai",
                                                 "email": "me@x.io"})
    auth.reset()


@pytest.fixture
def ids(home):
    s = Store()
    out = seed(s)
    s.close()
    return out


@pytest.fixture
def server(ids):
    srv = serve.make_server(paths.db_path(), 0, token=TOKEN)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.queue.shutdown()
    srv.server_close()


@pytest.fixture
def client(server):
    return Client(server)


def wait_job(client, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        _, out = client.json("GET", "/api/jobs")
        job = next(j for j in out["jobs"] if j["id"] == job_id)
        if job["state"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _reply(ids):
    c = {k: f"c{v}" for k, v in ids.items()}
    return {"title": "Boards", "requirements": [{"id": "r1", "text": "a dashboard"}],
            "options": [
                {"slot": "lean", "name": "Lite", "pitch": "small",
                 "capabilities": [{"id": c["Session login"], "role": "auth"},
                                  {"id": c["Team dashboard"], "role": "ui"}],
                 "coverage": [{"req": "r1", "kind": "catalog", "cap": c["Team dashboard"]}],
                 "glue": ["session middleware"], "new_work": ["theme editor"]},
                {"slot": "complete", "name": "Full", "pitch": "everything",
                 "capabilities": [{"id": c["Layout grid"], "role": "layout"},
                                  {"id": c["Activity feed"], "role": "events"}]}]}


@pytest.fixture
def fake_designer(ids, monkeypatch):
    seen = {}

    def fake(idea, rows, n_options, model, stack=None, pins=None):
        seen.update(idea=idea, pins=pins, n=n_options)
        return _reply(ids), 0.3
    monkeypatch.setattr(design, "_claude_design", fake)
    monkeypatch.setattr(design, "verify_links", lambda *a, **k: 0.0)
    return seen


# -- security -------------------------------------------------------------------------------
STUDIO_ROUTES = [("GET", "/api/catalog", None), ("POST", "/api/fit", {"ids": []}),
                 ("POST", "/api/estimate/design", {"options": 3}),
                 ("POST", "/api/designs", {"idea": "x"}), ("GET", "/api/designs", None),
                 ("GET", "/api/designs/x", None),
                 ("POST", "/api/designs/x/pick", {"option": "lean"}),
                 ("POST", "/api/kits/preview", {"composition": "x", "target": "new:x"}),
                 ("POST", "/api/kits", {"composition": "x", "target": "new:x"})]


@pytest.mark.parametrize("method,path,body", STUDIO_ROUTES)
def test_studio_endpoints_enforce_token_host_origin_and_json(client, method, path, body):
    assert client.call(method, path, body, token=None)[0] == 403
    assert client.call(method, path, body, token="wrong")[0] == 403
    assert client.call(method, path, body, host="evil.example")[0] == 403
    assert client.call(method, path, body, headers={"Origin": "http://evil.example"})[0] == 403
    if method == "POST":
        assert client.call(method, path, body, ctype="text/plain")[0] == 400
        assert client.call(method, path, raw=b"x" * 70000)[0] == 413


def test_studio_page_needs_no_token_and_has_the_csp(client, monkeypatch, tmp_path):
    status, headers, body = client.call("GET", "/studio", token=None)
    assert status == 200 and headers["Content-Security-Policy"] == serve.CSP
    monkeypatch.setattr(serve, "STUDIO_PAGE", tmp_path / "missing.html")
    status, _, body = client.call("GET", "/studio", token=None)
    assert status == 200 and b"studio page is not installed" in body
    assert client.call("GET", "/studio", token=None, host="evil.example")[0] == 403
    assert client.call("GET", "/studio/x", token=None)[0] == 404


# -- catalog and fit --------------------------------------------------------------------------
def test_catalog_shape_and_gating(client, ids, home):
    s = Store()
    s.conn.execute("UPDATE capabilities SET what=?, security_notes=? WHERE id=?",
                   ("Reads src/auth/login.py and `rm -rf /tmp/x`.", "see /etc/passwd",
                    ids["Session login"]))
    s.conn.commit()
    s.set_fit(ids["Session login"], "homelab", 0.8, "fits", "overlay")
    s.close()
    status, out = client.json("GET", "/api/catalog")
    assert status == 200 and set(out) == {"parts", "donors", "projects"}
    assert out["projects"] == ["homelab"]
    assert {d["donor"] for d in out["donors"]} == {"acme/authkit", "acme/boards", "evil/notify"}
    evil = next(d for d in out["donors"] if d["donor"] == "evil/notify")
    assert evil == {"donor": "evil/notify", "n": 2, "license": "AGPL-3.0", "level": "deny",
                    "review": True}
    p = next(x for x in out["parts"] if x["id"] == ids["Session login"])
    assert set(p) == {"id", "name", "donor", "license", "license_level", "license_review",
                      "novelty", "portability", "deepened", "what", "design_notes", "security",
                      "provides", "requires", "acceptance", "data_shapes", "key_values",
                      "files_count", "fits", "basis", "evidence"}
    assert "login.py" not in p["what"] and "rm -rf" not in p["what"]
    assert "/etc/passwd" not in p["security"]
    assert p["fits"] == [["homelab", 0.8]] and p["files_count"] == 1
    assert p["provides"] == ["auth:session"] and p["requires"] == ["store:users"]
    assert p["evidence"].startswith("**Evidence:**") and p["license_level"] == "ok"
    mail = next(x for x in out["parts"] if x["name"] == "Email notifier")
    assert mail["license_level"] == "deny" and mail["license_review"] is True
    assert len(out["parts"]) == 7


def test_catalog_leaves_out_retired_parts(client, ids):
    s = Store()
    s.conn.execute("UPDATE capabilities SET retired_at='2026-01-01' WHERE id=?",
                   (ids["Layout grid"],))
    s.conn.commit()
    s.close()
    _, out = client.json("GET", "/api/catalog")
    assert "Layout grid" not in {p["name"] for p in out["parts"]}


def test_fit_endpoint(client, ids):
    status, out = client.json("POST", "/api/fit", {
        "ids": [ids["Team dashboard"], ids["Layout grid"]], "roles": {}})
    assert status == 200
    assert set(out) == {"wired", "unmet", "settings", "external", "licenses", "projects",
                        "deepened", "deepen_cost", "kit_steps"}
    assert out["wired"][0]["provider"] == "Layout grid"
    assert out["unmet"][0]["candidates"][0]["id"] == ids["Session login"]
    assert client.json("POST", "/api/fit", {"ids": []})[0] == 200


@pytest.mark.parametrize("body", [{}, {"ids": "1"}, {"ids": ["1"]}, {"ids": [True]},
                                  {"ids": [0]}, {"ids": [99999]}, {"ids": list(range(1, 90))}])
def test_fit_rejects_bad_ids(client, body):
    status, out = client.json("POST", "/api/fit", body)
    assert status == 400 and out["error"]


def test_fit_rejects_retired_parts(client, ids):
    s = Store()
    s.conn.execute("UPDATE capabilities SET retired_at='2026-01-01' WHERE id=?",
                   (ids["Layout grid"],))
    s.conn.commit()
    s.close()
    assert client.json("POST", "/api/fit", {"ids": [ids["Layout grid"]]})[0] == 400


# -- estimate ---------------------------------------------------------------------------------
def test_design_estimate(client, ids):
    status, out = client.json("POST", "/api/estimate/design",
                              {"options": 4, "pin": [ids["Layout grid"]]})
    assert status == 200 and set(out) == {"low", "high", "minutes", "basis"}
    assert out["high"] > out["low"] > 0
    assert client.json("POST", "/api/estimate/design", {"options": 5})[0] == 400
    assert client.json("POST", "/api/estimate/design", {"options": 3, "pin": [99999]})[0] == 400
    assert client.json("POST", "/api/estimate/design", {})[0] == 200  # options default to 3


# -- designs ----------------------------------------------------------------------------------
def test_design_job_end_to_end_and_views(client, ids, fake_designer):
    status, out = client.json("POST", "/api/designs", {
        "idea": "team dashboard", "pin": [ids["Retry helper"]],
        "roles": {str(ids["Retry helper"]): "retries"}, "options": 2,
        "include_commodity": False})
    assert status == 200 and out["job"]["kind"] == "design"
    job = wait_job(client, out["job"]["id"])
    assert job["state"] == "done", job["error"]
    assert job["result"]["design_slug"] and job["cost_usd"] == pytest.approx(0.3)
    assert fake_designer["pins"] == [(f"c{ids['Retry helper']}", "retries")]

    _, lst = client.json("GET", "/api/designs")
    assert lst["designs"] == [{"slug": job["result"]["design_slug"], "title": "Boards",
                               "idea": "team dashboard", "created": lst["designs"][0]["created"],
                               "options": 2, "engine": "claude-p"}]
    status, d = client.json("GET", f"/api/designs/{job['result']['design_slug']}")
    assert status == 200 and set(d) == {"slug", "title", "idea", "requirements", "meta",
                                        "options"}
    assert d["requirements"] == [{"id": "r1", "text": "a dashboard"}]
    lean = next(o for o in d["options"] if o["slot"] == "lean")
    assert set(lean) == {"slot", "slot_label", "name", "slug", "pitch", "rationale", "stack",
                         "scores", "parts", "wiring", "coverage", "flags", "glue", "new_work"}
    assert set(lean["scores"]) == {"reuse", "fit"} and lean["slot_label"] == "Lean"
    retry = next(p for p in lean["parts"] if p["name"] == "Retry helper")
    assert retry == {"id": ids["Retry helper"], "name": "Retry helper", "donor": "acme/authkit",
                     "license": "MIT", "role": "retries", "pinned": True}
    assert set(lean["wiring"]) == {"wired", "unmet", "counts"}
    assert set(lean["wiring"]["counts"]) == {"wired", "unmet", "settings", "external"}
    assert all(isinstance(x, str) for x in lean["wiring"]["wired"] + lean["wiring"]["unmet"])
    assert lean["glue"] == ["session middleware"] and lean["new_work"] == ["theme editor"]
    assert client.json("GET", "/api/designs/nope")[0] == 404
    assert client.json("GET", "/api/designs/a%2Fb")[0] == 404


@pytest.mark.parametrize("body", [
    {}, {"idea": ""}, {"idea": "x" * 2001}, {"idea": "x", "options": 5},
    {"idea": "x", "options": 1}, {"idea": "x", "options": "3"}, {"idea": "x", "pin": "17"},
    {"idea": "x", "pin": ["c1"]}, {"idea": "x", "pin": [99999]}, {"idea": "x", "roles": []},
    {"idea": "x", "roles": {"1": 5}}])
def test_design_inputs_are_validated(client, ids, body):
    status, out = client.json("POST", "/api/designs", body)
    assert status == 400 and out["error"]
    assert client.json("GET", "/api/jobs")[1]["jobs"] == []


def test_design_refuses_denied_and_retired_pins(client, ids):
    status, out = client.json("POST", "/api/designs",
                              {"idea": "x", "pin": [ids["Email notifier"]]})
    assert status == 400 and "denies" in out["error"]


def test_design_job_failure_reports_the_cause(client, ids, monkeypatch):
    def boom(*a, **k):
        raise design.DesignError("claude-p returned no JSON design ($0.2000 spent)", 0.2)
    monkeypatch.setattr(design, "_claude_design", boom)
    _, out = client.json("POST", "/api/designs", {"idea": "dashboard"})
    job = wait_job(client, out["job"]["id"])
    assert job["state"] == "failed" and "no JSON design" in job["error"]
    assert job["cost_usd"] == pytest.approx(0.2)


def _make_design(client, fake_designer):
    _, out = client.json("POST", "/api/designs", {"idea": "team dashboard", "options": 2})
    return wait_job(client, out["job"]["id"])["result"]["design_slug"]


def test_pick_returns_the_composition_summary(client, ids, fake_designer):
    slug = _make_design(client, fake_designer)
    status, out = client.json("POST", f"/api/designs/{slug}/pick",
                              {"option": "lean", "target": "new:Board Lite"})
    assert status == 200
    comp = out["composition"]
    assert set(comp) == {"slug", "name", "target_kind", "target_slug", "problems"}
    assert comp["name"] == "Lite" and comp["target_kind"] == "new"
    assert comp["problems"] == []
    status, out = client.json("POST", f"/api/designs/{slug}/pick", {"option": "lean",
                                                                    "target": "homelab"})
    assert out["composition"]["target_kind"] == "existing"
    assert out["composition"]["target_slug"] == "homelab"
    status, out = client.json("POST", f"/api/designs/{slug}/pick", {"option": "lean"})
    assert status == 200 and out["composition"]["target_kind"] == "new"


def test_pick_validation(client, ids, fake_designer, tmp_path):
    slug = _make_design(client, fake_designer)
    pick = f"/api/designs/{slug}/pick"
    assert client.json("POST", pick, {})[0] == 400
    assert client.json("POST", pick, {"option": "bold"})[0] == 400
    assert client.json("POST", pick, {"option": "lean", "target": "relative/dir"})[0] == 400
    assert client.json("POST", pick, {"option": "lean", "target": "nobody"})[0] == 400
    assert client.json("POST", "/api/designs/nope/pick", {"option": "lean"})[0] == 404
    assert client.json("POST", pick, {"option": "lean", "target": str(tmp_path)})[0] == 200
    assert client.json("GET", pick)[0] == 404  # the pick route is POST only


# -- kits -------------------------------------------------------------------------------------
def _picked(client, fake_designer, target="new:Board Lite"):
    slug = _make_design(client, fake_designer)
    _, out = client.json("POST", f"/api/designs/{slug}/pick",
                         {"option": "complete", "target": target})
    return out["composition"]["slug"]


def test_kit_preview_is_a_dry_run(client, ids, fake_designer):
    comp = _picked(client, fake_designer)
    status, out = client.json("POST", "/api/kits/preview",
                              {"composition": comp, "target": "new:preview-app"})
    assert status == 200
    assert set(out) == {"target", "steps", "warnings", "refusal", "deepen", "plan", "decisions",
                        "plan2", "reconcile"}
    assert set(out["reconcile"]) == {"cached", "low", "high", "status", "changes"}
    assert set(out["target"]) == {"dir", "kind", "stack", "test_cmd"}
    assert out["target"]["kind"] == "new" and out["refusal"] == []
    assert [s["n"] for s in out["steps"]] == [0, 1, 2, 3]  # + build-fresh for the unlabelled r1
    assert set(out["steps"][1]) == {"n", "kind", "title", "donor", "deepened", "guard_note",
                                    "needs", "checks", "spec_head"}
    assert out["deepen"]["needed"] == 2 and out["deepen"]["cost"] == pytest.approx(0.022)
    assert "secret/x.py" not in str(out) and "src/" not in out["steps"][1]["spec_head"]
    assert not (paths.data_dir() / "new" / "preview-app").exists()
    s = Store()
    assert s.conn.execute("SELECT COUNT(*) FROM kits").fetchone()[0] == 0
    assert s.get_composition(comp).status == "proposed"
    s.close()


def test_kit_preview_reports_refusals_and_validates(client, ids, fake_designer, tmp_path):
    comp = _picked(client, fake_designer)
    s = Store()
    s.conn.execute("UPDATE capabilities SET retired_at='2026-01-01' WHERE id=?",
                   (ids["Layout grid"],))
    s.conn.commit()
    s.close()
    status, out = client.json("POST", "/api/kits/preview",
                              {"composition": comp, "target": "new:preview-app"})
    assert status == 200 and any("retired" in r for r in out["refusal"])
    assert client.json("POST", "/api/kits/preview", {"composition": comp})[0] == 400
    assert client.json("POST", "/api/kits/preview",
                       {"composition": comp, "target": "rel/path"})[0] == 400
    assert client.json("POST", "/api/kits/preview",
                       {"composition": comp, "target": str(tmp_path / "missing")})[0] == 400
    assert client.json("POST", "/api/kits/preview",
                       {"composition": "nope", "target": "new:x"})[0] == 404
    assert client.json("POST", "/api/kits/preview", {"target": "new:x"})[0] == 400


def test_kit_job_writes_a_kit_to_a_new_target(client, ids, fake_designer):
    comp = _picked(client, fake_designer)
    status, out = client.json("POST", "/api/kits", {
        "composition": comp, "target": "new:served-kit", "deepen": False, "force": False})
    assert status == 200 and out["job"]["kind"] == "kit"
    job = wait_job(client, out["job"]["id"])
    assert job["state"] == "done", job["error"]
    kd = paths.data_dir() / "new" / "served-kit" / ".reincarnate"
    assert job["result"]["kit_dir"] == str(kd) and (kd / "run.sh").is_file()
    assert job["result"]["steps"] == 4  # + build-fresh for the unlabelled r1
    again = wait_job(client, client.json("POST", "/api/kits", {
        "composition": comp, "target": "new:served-kit"})[1]["job"]["id"])
    assert again["state"] == "done"  # nothing ticked, nothing edited: a plain re-export
    (kd / "specs" / next((kd / "specs").iterdir()).name).write_text("hand edit\n")
    refused = wait_job(client, client.json("POST", "/api/kits", {
        "composition": comp, "target": "new:served-kit"})[1]["job"]["id"])
    assert refused["state"] == "failed" and "edited by hand" in refused["error"]


def test_kit_validation(client, ids, fake_designer):
    comp = _picked(client, fake_designer)
    assert client.json("POST", "/api/kits", {"composition": comp})[0] == 400
    assert client.json("POST", "/api/kits", {"composition": comp, "target": "../x"})[0] == 400
    assert client.json("POST", "/api/kits", {"composition": "nope", "target": "new:x"})[0] == 404
    assert [j for j in client.json("GET", "/api/jobs")[1]["jobs"] if j["kind"] == "kit"] == []


def test_kit_decisions_in_preview_and_answers_on_write(client, ids, fake_designer, monkeypatch):
    from reincarnate import kitplan
    from test_kitplan import Runner
    reply = {"steps": [{"n": 1, "in_scope": ["x"]}], "open_decisions": [
        {"id": "D1", "question": "Can anonymous visitors see the board?", "why": "access",
         "options": ["Signed-in only", "Anyone"], "default": "Signed-in only", "steps": [1]}]}
    runner = Runner(reply)
    monkeypatch.setattr(kitplan, "_default_runner", runner)
    comp = _picked(client, fake_designer)
    _, pv = client.json("POST", "/api/kits/preview", {"composition": comp, "target": "new:dec"})
    assert pv["decisions"] == [{**reply["open_decisions"][0], "answer": None, "source": None}]
    assert any("unanswered" in w for w in pv["warnings"])
    for bad in ({"D1": 5}, {"D1": ""}, {"D1": "x" * 2001}, {"nope": "x"}, ["D1"]):
        assert client.json("POST", "/api/kits", {"composition": comp, "target": "new:dec",
                                                 "answers": bad})[0] == 400
    ok = client.json("POST", "/api/kits", {"composition": comp, "target": "new:dec",
                                           "answers": {"D1": "Anyone"}})
    assert ok[0] == 200
    job = wait_job(client, ok[1]["job"]["id"])
    assert job["state"] == "done", job["error"]
    # the preview's plan served the write; the answer made the second (contracts) pass run
    assert len(runner.prompts) == 2
    contracts = (paths.data_dir() / "new" / "dec" / ".reincarnate" / "CONTRACTS.md").read_text()
    assert "Decided: Anyone" in contracts
    _, pv2 = client.json("POST", "/api/kits/preview", {"composition": comp, "target": "new:dec"})
    assert pv2["decisions"][0]["answer"] == "Anyone" and pv2["decisions"][0]["source"] == "user"
    bad_id = client.json("POST", "/api/kits", {"composition": comp, "target": "new:dec",
                                               "answers": {"D8": "x"}})
    assert wait_job(client, bad_id[1]["job"]["id"])["state"] == "failed"

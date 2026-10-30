import http.client
import json
import threading
import time

import pytest

from conftest import make_dossier
from reincarnate import auth, comprehend, jobs, paths, serve
from reincarnate.models import Profile, Project
from reincarnate.store import Store

TOKEN = "tok-for-tests"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.setattr(auth, "_probe", lambda: {"logged_in": True, "method": "claude.ai",
                                                 "email": "me@x.io"})
    auth.reset()


@pytest.fixture
def server(home):
    Store().close()
    srv = serve.make_server(paths.db_path(), 0, token=TOKEN)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.queue.shutdown()
    srv.server_close()


class Client:
    def __init__(self, srv):
        self.port = srv.server_address[1]

    def call(self, method, path, body=None, *, headers=None, raw=None, token=TOKEN, host=None,
             ctype="application/json"):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Reincarnate-Token"] = token
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if method == "POST":
            if ctype:
                h["Content-Type"] = ctype
            h["Content-Length"] = str(len(data or b""))
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        conn.endheaders(data if method == "POST" else None)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), payload

    def json(self, method, path, body=None, **kw):
        status, _, payload = self.call(method, path, body, **kw)
        return status, json.loads(payload)


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


def install_fake_comprehend(monkeypatch, gate=None):
    def fake(store, url, *, model=None, mode="excerpt", force=False, progress=None, **kw):
        progress("clone")
        progress("excerpt")
        progress("model pass")
        if gate is not None:
            assert gate.wait(10)
        owner, repo = url.rsplit("/", 2)[-2:]
        d = store.upsert_dossier(owner, repo, url, "sha1",
                                 make_dossier(("alpha", "does alpha")), model=model,
                                 cost_usd=0.02)
        return {"dossier_id": d.id, "donor": f"{owner}/{repo}", "sha": "sha1", "capabilities": 1,
                "cost_usd": 0.02, "parsed": True}
    monkeypatch.setattr(comprehend, "comprehend_url", fake)


# -- security ---------------------------------------------------------------------------
@pytest.mark.parametrize("token", [None, "wrong", TOKEN + "x"])
def test_api_needs_the_token(client, token):
    for method, path in (("GET", "/api/state"), ("GET", "/api/jobs"), ("POST", "/api/jobs")):
        status, out = client.json(method, path, {} if method == "POST" else None, token=token)
        assert status == 403 and "token" in out["error"]


def test_bad_host_is_refused(client):
    for host in ("evil.example:%d" % client.port, "127.0.0.1:1", "127.0.0.1"):
        assert client.json("GET", "/api/state", host=host)[0] == 403
    status, _, _ = client.call("GET", "/", host="rebind.example")
    assert status == 403
    assert client.json("GET", "/api/state", host=f"localhost:{client.port}")[0] == 200


def test_foreign_origin_is_refused(client):
    for origin in ("http://evil.example", "null", "https://127.0.0.1:%d" % client.port,
                   "http://127.0.0.1:1"):
        assert client.json("GET", "/api/state", headers={"Origin": origin})[0] == 403
    own = f"http://127.0.0.1:{client.port}"
    assert client.json("GET", "/api/state", headers={"Origin": own})[0] == 200
    assert client.call("GET", "/", headers={"Origin": "http://evil.example"})[0] == 403


def test_post_needs_a_json_content_type(client):
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
        status, out = client.json("POST", "/api/estimate", {"repos": ["a/b"]}, ctype=ctype)
        assert status == 400 and "application/json" in out["error"]
    ok = client.json("POST", "/api/estimate", {"repos": ["a/b"]},
                     ctype="application/json; charset=utf-8")
    assert ok[0] == 200


def test_body_cap_is_64kb(client):
    big = b'{"repos": ["' + b"a" * (64 * 1024) + b'"]}'
    status, out = client.json("POST", "/api/estimate", raw=big)
    assert status == 413 and "64 KB" in out["error"]
    just = json.dumps({"repos": ["a/b"], "pad": "x" * 1000}).encode()
    assert client.json("POST", "/api/estimate", raw=just)[0] == 200


def test_malformed_bodies(client):
    assert client.json("POST", "/api/estimate", raw=b"{nope")[0] == 400
    assert client.json("POST", "/api/estimate", raw=b"[1, 2]")[0] == 400
    assert client.json("POST", "/api/estimate", raw=b"")[0] == 400  # no repos


def test_other_methods_and_paths(client):
    assert client.json("PUT", "/api/state")[0] == 405
    assert client.json("GET", "/api/nothing")[0] == 404
    assert client.json("GET", "/elsewhere", token=None)[0] == 404
    assert client.json("POST", "/api/jobs/999/cancel", {})[0] == 404
    assert client.json("POST", "/api/jobs/999/retry", {})[0] == 404


# -- the page ---------------------------------------------------------------------------
def test_page_needs_no_token_and_carries_security_headers(client):
    status, headers, body = client.call("GET", "/", token=None)
    assert status == 200 and body
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    csp = headers["Content-Security-Policy"]
    assert csp == serve.CSP and "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert client.call("GET", "/?x=1", token=None)[0] == 200


def test_page_falls_back_to_a_placeholder_when_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "PAGE", tmp_path / "missing.html")
    status, _, body = client.call("GET", "/", token=None)
    assert status == 200 and b"not installed" in body


def test_server_binds_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


# -- contract ---------------------------------------------------------------------------
def test_state_keys_on_an_empty_catalog(client):
    status, out = client.json("GET", "/api/state")
    assert status == 200
    assert set(out) == {"summary", "auth", "profile", "projects", "own", "upkeep", "recent"}
    assert set(out["summary"]) == {"caps", "donors", "nov", "deep", "spend", "pinned",
                                   "dossiers", "license_review"}
    assert out["summary"]["nov"] == {"d": 0, "p": 0, "c": 0, "u": 0}
    # the first state call never waits on `claude auth status`: it says "checking" and probes
    # in the background; the next call has the answer
    assert out["auth"].get("checking") is True or out["auth"]["logged_in"] is True
    for _ in range(50):
        status, out2 = client.json("GET", "/api/state")
        if out2["auth"].get("logged_in") is not None:
            break
        time.sleep(0.05)
    assert out2["auth"]["logged_in"] is True
    assert set(out["profile"]) == {"name", "stack", "deny", "warn"}
    assert out["upkeep"] == [] and out["own"] == [] and out["recent"] == []


def test_state_summarizes_the_catalog(client, home):
    s = Store()
    s.save_profile(Profile(name="me", license_policy={"deny": ["GPL-3.0"], "warn": ["unknown"]}))
    d = make_dossier(("alpha", "a"), ("beta", "b"), ("gamma", "c"))
    for cap, nov in zip(d["capabilities"], ("distinctive", "pattern", "")):
        cap["novelty"] = nov
    s.upsert_dossier("me", "mine", "https://x/me/mine", "s1", d, model="m", cost_usd=0.5,
                     source="claude-p")
    d2 = make_dossier(("delta", "d"))
    d2["dependencies"]["license"] = "GPL-3.0"
    s.upsert_dossier("them", "gpl", "https://x/them/gpl", "unknown", d2, cost_usd=0.25)
    cap = s.list_capabilities(current_only=True)[0]
    s.set_capability_fields(
        cap.id, deepened={"at": "2026-01-01T00:00:00Z", "sha": "sha", "model": "m"})
    s.close()
    store = Store()
    store.add_project(Project(slug="p", path="/tmp", repo_url="https://github.com/me/p.git"))
    jid = store.add_job("comprehend", "x/broken", "{}", "[]", "queued")
    store.update_job(jid, state="failed", error="git clone failed: nope")
    store.close()

    _, out = client.json("GET", "/api/state")
    sm = out["summary"]
    assert (sm["caps"], sm["donors"], sm["dossiers"]) == (4, 2, 2)
    assert sm["nov"] == {"d": 1, "p": 1, "c": 0, "u": 2}
    assert sm["spend"] == 0.75 and sm["pinned"] == 1 and sm["license_review"] == 1
    assert sm["deep"] == 1
    assert [o["donor"] for o in out["own"]] == ["me/mine"]
    assert out["own"][0]["n"] == 3 and out["own"][0]["nov"]["d"] == 1
    assert set(out["own"][0]) == {"donor", "n", "license", "summary", "nov"}
    assert out["profile"]["deny"] == ["GPL-3.0"]
    assert out["projects"][0]["slug"] == "p"
    keys = {u["key"]: u for u in out["upkeep"]}
    assert set(keys) == {"unknown-commit", "unread-in-code", "license-review", "failed-imports"}
    for u in out["upkeep"]:
        assert set(u) == {"key", "title", "why", "items", "cmd"} and u["items"] and u["cmd"]
    assert keys["unknown-commit"]["items"] == ["them/gpl"]
    assert "reincarnate comprehend https://github.com/x/broken" in keys["failed-imports"]["cmd"]
    assert {r["donor"] for r in out["recent"]} == {"me/mine", "them/gpl"}
    assert set(out["recent"][0]) == {"donor", "at", "source", "n"}


def test_estimate_endpoint(client):
    status, out = client.json("POST", "/api/estimate", {
        "repos": ["pallets/click", "https://github.com/pallets/flask", "pallets/click",
                  "git@github.com:a/b.git", "/etc/passwd", "http://evil.example/a/b", 5, ""],
        "mode": "excerpt", "model": "haiku", "enrich": True})
    assert status == 200
    assert out["repos"] == ["pallets/click", "pallets/flask"]
    assert out["invalid"] == ["git@github.com:a/b.git", "/etc/passwd",
                              "http://evil.example/a/b", "5", ""]
    assert set(out) == {"repos", "invalid", "low", "high", "per_repo", "minutes", "basis"}
    assert 0 < out["low"] < out["high"] and out["basis"]
    assert client.json("POST", "/api/estimate", {"repos": ["a/b"], "mode": "x"})[0] == 400
    assert client.json("POST", "/api/estimate", {"repos": ["a/b"], "model": "x"})[0] == 400
    assert client.json("POST", "/api/estimate", {"repos": []})[0] == 400
    assert client.json("POST", "/api/estimate", {"repos": ["a/b"] * 51})[0] == 400


def test_job_lifecycle_through_the_api(client, monkeypatch):
    install_fake_comprehend(monkeypatch)
    status, out = client.json("POST", "/api/jobs", {
        "repos": ["pallets/click", "bad repo", "pallets/flask"], "mode": "excerpt",
        "model": "haiku", "enrich": False})
    assert status == 200 and out["invalid"] == ["bad repo"]
    first = out["jobs"][0]
    assert set(first) >= {"id", "kind", "target", "params", "state", "stage", "stages", "created",
                          "started", "finished", "elapsed_s", "cost_usd", "error", "result"}
    assert [j["target"] for j in out["jobs"]] == ["pallets/click", "pallets/flask"]
    done = wait_job(client, first["id"])
    wait_job(client, out["jobs"][1]["id"])
    assert done["state"] == "done" and done["cost_usd"] == 0.02
    assert set(done["result"]) >= {"dossier_id", "donor", "sha", "capabilities", "nov",
                                   "reconciled", "skipped"}
    assert {s["state"] for s in done["stages"]} == {"done", "skipped"}

    status, out = client.json("GET", "/api/jobs")
    assert status == 200 and [j["id"] for j in out["jobs"]][0] > first["id"]  # newest first

    status, out = client.json("POST", f"/api/jobs/{first['id']}/retry", {})
    assert status == 200 and out["job"]["id"] != first["id"]
    assert out["job"]["params"] == first["params"]
    wait_job(client, out["job"]["id"])

    _, state = client.json("GET", "/api/state")
    assert state["summary"]["donors"] == 2 and state["summary"]["spend"] == 0.04
    assert {r["donor"] for r in state["recent"]} == {"pallets/click", "pallets/flask"}


def test_cancel_through_the_api(client, monkeypatch):
    gate = threading.Event()
    install_fake_comprehend(monkeypatch, gate)
    _, out = client.json("POST", "/api/jobs", {"repos": ["a/one", "a/two"]})
    one, two = out["jobs"]
    end = time.time() + 10
    while client.json("GET", "/api/jobs")[1]["jobs"][-1]["stage"] != "model pass":
        assert time.time() < end
        time.sleep(0.02)
    status, out = client.json("POST", f"/api/jobs/{two['id']}/cancel", {})
    assert status == 200 and out["job"]["state"] == "cancelled"
    status, out = client.json("POST", f"/api/jobs/{one['id']}/cancel", {})
    assert out["job"]["state"] == "running"
    gate.set()
    assert wait_job(client, one["id"])["state"] == "cancelled"


def test_failed_job_reports_the_cause(client, monkeypatch):
    def boom(store, url, **kw):
        raise RuntimeError("Failed to authenticate. OAuth token has expired")
    monkeypatch.setattr(comprehend, "comprehend_url", boom)
    _, out = client.json("POST", "/api/jobs", {"repos": ["a/b"]})
    job = wait_job(client, out["jobs"][0]["id"])
    assert job["state"] == "failed"
    assert job["error"].endswith("Run `claude auth login` in a terminal, then retry.")


def test_other_job_kinds_are_accepted(client, monkeypatch):
    from reincarnate import deepen, enrich
    monkeypatch.setattr(enrich, "enrich", lambda store, **kw: {"updated": 1, "cost_usd": 0.0})
    monkeypatch.setattr(deepen, "run_cli", lambda store, args: {"deepened": [1], "failed": []})
    _, out = client.json("POST", "/api/jobs", {"kind": "enrich", "repos": ["a/b"]})
    assert out["jobs"][0]["kind"] == "enrich"
    assert wait_job(client, out["jobs"][0]["id"])["state"] == "done"
    _, out = client.json("POST", "/api/jobs", {"kind": "deepen", "composition": "my-app"})
    assert wait_job(client, out["jobs"][0]["id"])["state"] == "done"
    assert client.json("POST", "/api/jobs", {"kind": "deepen"})[0] == 400
    assert client.json("POST", "/api/jobs", {"kind": "bogus", "repos": ["a/b"]})[0] == 400


def test_register_a_project(client, tmp_path):
    proj = tmp_path / "my-app"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "x"}')
    status, out = client.json("POST", "/api/projects",
                              {"path": str(proj), "slug": "my-app", "goal": "ship it"})
    assert status == 200
    assert out["project"]["slug"] == "my-app" and out["project"]["goal"] == "ship it"
    assert out["project"]["path"] == str(proj.resolve())
    _, state = client.json("GET", "/api/state")
    assert [p["slug"] for p in state["projects"]] == ["my-app"]
    assert client.json("POST", "/api/projects", {"path": str(tmp_path / "nope")})[0] == 400
    assert client.json("POST", "/api/projects", {})[0] == 400
    assert client.json("POST", "/api/projects", {"path": str(proj), "slug": "Bad Slug"})[0] == 400


def test_request_log_has_neither_token_nor_query(client, capsys):
    client.call("GET", "/api/state?secret=abc")
    err = capsys.readouterr().err
    assert "GET /api/state 200" in err and TOKEN not in err and "secret" not in err


def test_port_fallback_skips_a_busy_port(home):
    Store().close()
    a = serve.make_server(paths.db_path(), 0, token="a", queue=jobs.JobQueue(
        paths.db_path(), start=False))
    busy = a.server_address[1]
    with pytest.raises(OSError):
        serve.make_server(paths.db_path(), busy, token="b")
    a.server_close()


def test_normalize_repo():
    assert serve.normalize_repo("https://github.com/Pallets/Click.git") == "Pallets/Click"
    assert serve.normalize_repo(" a/b ") == "a/b"
    for bad in ("git@github.com:a/b.git", "http://github.com/a/b", "https://gitlab.com/a/b",
                "a", "a/b/c", "../x", "./a/b", "~/a/b"):
        with pytest.raises(ValueError):
            serve.normalize_repo(bad)

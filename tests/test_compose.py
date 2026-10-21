import json
from types import SimpleNamespace

import pytest

from reincarnate import claude_runner, compose
from reincarnate.models import Profile, Project

_RANK = {"high": 0, "medium": 1, "low": 2}


def _dossier(license_, caps, arch=""):
    return {
        "summary": "A donor.",
        "architecture": arch,
        "dependencies": {"license": license_},
        "capabilities": [
            {"name": n, "what": w, "portability": p, "files": ["secret/source.py"]}
            for n, w, p in caps
        ],
    }


@pytest.fixture
def seeded(store):
    store.add_project(Project(slug="homelab", path="/x/homelab", goal="monitor servers"))
    store.add_project(Project(slug="notes", path="/x/notes", goal="markdown notes"))
    store.save_profile(Profile(name="dan", stack=["python"],
                               license_policy={"deny": ["AGPL-3.0"], "warn": ["unknown"]}))
    store.upsert_dossier("o", "mit-repo", "https://github.com/o/mit-repo", "a1", _dossier(
        "MIT",
        [("Status dashboard", "dashboard rendering service tiles", "low"),
         ("Health checks", "periodic health checks with retries", "high"),
         ("Config loader", "loads yaml config for dashboard", "medium")],
        arch="A poller feeds a cache that the dashboard reads."))
    store.upsert_dossier("o", "agpl-repo", "https://github.com/o/agpl-repo", "b1", _dossier(
        "AGPL-3.0",
        [("Uptime probes", "health checks over http", "medium"),
         ("Alert router", "routes alerts to channels", "high")]))
    return store


def _ids(store):
    return {c.name: c.id for c in store.list_capabilities()}


def _fake_invoke(payload):
    def fake(argv, cwd, env, timeout):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        out = json.dumps({"type": "result", "result": text, "total_cost_usd": 0.01})
        return 0, out.encode(), b""
    return fake


def _ns(**kw):
    base = {"idea": [], "caps": None, "target": None, "engine": "offline", "json": False}
    base.update(kw)
    return SimpleNamespace(**base)


# -- retrieval / helpers ------------------------------------------------------------------
def test_retrieve_caps_first_then_fts_then_fill(seeded):
    ids = _ids(seeded)
    rows = compose.retrieve(seeded, "alert", caps=[ids["Config loader"]])
    assert rows[0]["capability_id"] == ids["Config loader"]
    got = [r["capability_id"] for r in rows]
    assert len(got) == len(set(got)) == 5  # fill tops up to all 5 (< 8)
    assert set(rows[0]) >= {"capability_id", "donor", "license", "name", "what", "portability",
                            "portability_notes", "security_notes", "fit"}
    assert "files" not in rows[0]


def test_retrieve_respects_limit_but_keeps_caps(seeded):
    ids = _ids(seeded)
    rows = compose.retrieve(seeded, "health checks", caps=[ids["Alert router"]], limit=1)
    assert [r["capability_id"] for r in rows][0] == ids["Alert router"]


def test_slugs(seeded):
    assert compose.slugify("My App!  v2") == "my-app-v2"
    assert compose.unique_slug(seeded, "My App") == "my-app"
    compose.propose(seeded, "anything", engine="offline", name="My App")
    assert compose.unique_slug(seeded, "My App") == "my-app-2"


def test_extract_json_object():
    assert compose.extract_json_object('x ```json\n{"a": 1}\n``` y') == {"a": 1}
    assert compose.extract_json_object('{"a": 1} then {"b": {"c": 2}}') == {"b": {"c": 2}}
    assert compose.extract_json_object("no json here") is None


# -- offline engine -----------------------------------------------------------------------
def test_offline_propose_roundtrip_and_flags(seeded):
    c = compose.propose(seeded, "dashboard with health checks", engine="offline")
    caps = c.proposal["capabilities"]
    assert len(caps) >= 2
    assert [s["order"] for s in caps] == list(range(1, len(caps) + 1))
    ranks = [_RANK[s["portability"]] for s in caps]
    assert ranks == sorted(ranks)
    assert all(s["role"] == "component" for s in caps)
    back = seeded.get_composition(c.slug)
    assert back is not None and back.to_dict() == c.to_dict()
    assert c.status == "proposed" and c.target_kind == "new"
    agpl = {s["capability_id"] for s in caps if s["donor"] == "o/agpl-repo"}
    assert agpl, "AGPL capabilities should be retrieved for this idea"
    deny = {f["capability_id"] for f in c.proposal["license_flags"] if f["level"] == "deny"}
    assert deny == agpl
    assert "secret/source.py" not in json.dumps(c.to_dict())


# -- claude-p engine ----------------------------------------------------------------------
def test_claude_hallucinated_id_dropped_and_fewer_than_two(seeded, monkeypatch):
    real = _ids(seeded)["Health checks"]
    monkeypatch.setattr(claude_runner, "_invoke", _fake_invoke({
        "name": "Pulse", "pitch": "watch things",
        "capabilities": [{"capability_id": 999, "role": "core", "order": 1},
                         {"capability_id": real, "role": "checker", "order": 2}]}))
    c = compose.propose(seeded, "dashboard with health checks", engine="claude-p")
    assert [s["capability_id"] for s in c.proposal["capabilities"]] == [real]
    assert c.proposal["capabilities"][0]["order"] == 1
    assert compose._FEWER in c.proposal["problems"]
    saved = seeded.get_composition(c.slug)
    assert saved.status == "proposed" and compose._FEWER in saved.proposal["problems"]
    assert c.name == "Pulse" and c.pitch == "watch things" and c.slug == "pulse"


def test_claude_glue_and_gap_normalized(seeded, monkeypatch):
    ids = _ids(seeded)
    payload = {
        "name": "Pulse",
        "capabilities": [{"capability_id": ids["Status dashboard"], "role": "ui", "order": 5},
                         {"capability_id": str(ids["Health checks"]), "order": 2}],
        "glue": "Wire checks into the dashboard.",
        "gaps": [{"need": "auth", "suggested_donors": "o/x", "build_fresh": "false"}],
        "risks": "flaky probes",
    }
    monkeypatch.setattr(claude_runner, "_invoke", _fake_invoke("Here:\n```json\n"
                                                               + json.dumps(payload) + "\n```"))
    c = compose.propose(seeded, "dashboard with health checks", engine="claude-p")
    p = c.proposal
    assert p["glue"] == "Wire checks into the dashboard."
    assert p["gaps"] == [{"need": "auth", "suggested_donors": ["o/x"], "build_fresh": False}]
    assert isinstance(p["gaps"][0]["build_fresh"], bool)
    assert p["risks"] == ["flaky probes"]
    assert [(s["capability_id"], s["order"], s["role"]) for s in p["capabilities"]] == [
        (ids["Health checks"], 1, "component"), (ids["Status dashboard"], 2, "ui")]
    assert p["problems"] == []


def test_claude_garbage_falls_back_offline(seeded, monkeypatch):
    monkeypatch.setattr(claude_runner, "_invoke", _fake_invoke("I cannot help with that."))
    c = compose.propose(seeded, "dashboard with health checks", engine="claude-p")
    assert len(c.proposal["capabilities"]) >= 2
    assert any("fell back" in n for n in c.proposal["notes"])
    assert c.proposal["engine"] == "offline"


def test_claude_prompt_receives_ids(seeded, monkeypatch):
    seen = {}

    def fake(argv, cwd, env, timeout):
        seen["prompt"] = argv[argv.index("-p") + 1]
        seen["tools"] = argv[argv.index("--tools") + 1]
        return 0, b"{}", b""

    monkeypatch.setattr(claude_runner, "_invoke", fake)
    compose.propose(seeded, "dashboard", engine="claude-p")
    assert f"id={_ids(seeded)['Status dashboard']}" in seen["prompt"]
    assert seen["tools"] == ""


# -- targets ------------------------------------------------------------------------------
def test_targets(seeded):
    c = compose.propose(seeded, "dashboard health", engine="offline", target="new:My App")
    assert (c.target_kind, c.target_slug) == ("new", "my-app")
    c = compose.propose(seeded, "dashboard health", engine="offline", target="unknown-slug")
    assert any(p.startswith(compose._UNKNOWN_TARGET) for p in c.proposal["problems"])
    c = compose.propose(seeded, "dashboard health", engine="offline", target="homelab")
    assert (c.target_kind, c.target_slug) == ("existing", "homelab")
    c = compose.propose(seeded, "dashboard health", engine="offline", name="Solo")
    assert (c.target_kind, c.target_slug) == ("new", "solo")


def test_license_warn_for_unknown(seeded):
    rows = [{"capability_id": 1, "donor": "a/b", "license": "", "name": "x", "what": "",
             "portability": "high"},
            {"capability_id": 2, "donor": "c/d", "license": "MIT", "name": "y", "what": "",
             "portability": "low"}]
    prop, problems = compose.validate_proposal(
        seeded, compose.offline_proposal(rows, "i"), rows, seeded.get_profile(), target=None)
    assert prop["license_flags"] == [
        {"capability_id": 1, "name": "x", "donor": "a/b", "license": "unknown",
         "level": "warn"}]
    assert problems == []


# -- whole-donor --------------------------------------------------------------------------
def test_reincarnate_donor(seeded):
    c = compose.reincarnate_donor(seeded, "o/mit-repo", target="new:Clone")
    names = [s["name"] for s in c.proposal["capabilities"]]
    assert names == ["Health checks", "Config loader", "Status dashboard"]
    assert c.proposal["glue"].startswith("Reference architecture")
    assert "poller" in c.proposal["glue"]
    assert (c.target_kind, c.target_slug) == ("new", "clone")
    assert c.name == "Reincarnate o/mit-repo" and c.idea == "Rebuild o/mit-repo natively"
    assert c.proposal["license_flags"] == []
    with pytest.raises(ValueError):
        compose.reincarnate_donor(seeded, "o/nope", target=None)


def test_reincarnate_agpl_flags(seeded):
    c = compose.reincarnate_donor(seeded, "o/agpl-repo", target="homelab")
    assert [f["level"] for f in c.proposal["license_flags"]] == ["deny", "deny"]
    assert c.target_kind == "existing"


# -- CLI ----------------------------------------------------------------------------------
def test_run_cli_paths(seeded):
    out = compose.run_cli(seeded, _ns(idea=["dashboard", "with", "health", "checks"],
                                      caps="1, 2"))
    assert out["slug"] and out["id"] and "1. " in out["text"]
    picked = {s["capability_id"] for s in out["composition"]["proposal"]["capabilities"]}
    assert {1, 2} <= picked

    listed = compose.run_cli(seeded, _ns(idea=["list"]))
    assert [i["slug"] for i in listed["items"]] == [out["slug"]]
    assert out["slug"] in listed["text"]

    shown = compose.run_cli(seeded, _ns(idea=["show", out["slug"]]))
    assert shown["composition"]["id"] == out["id"]
    assert "1. " in shown["text"] and "2. " in shown["text"]
    assert "license flags:" in shown["text"]
    assert compose.run_cli(seeded, _ns(idea=["show", str(out["id"])]))["composition"]
    assert "error" in compose.run_cli(seeded, _ns(idea=["show", "nope"]))
    assert "error" in compose.run_cli(seeded, _ns(idea=[]))


def test_run_cli_reincarnate_route(seeded):
    args = SimpleNamespace(reincarnate_donor=True, repo="o/mit-repo", into="new:Clone",
                           json=False)
    out = compose.run_cli(seeded, args)
    assert out["composition"]["name"] == "Reincarnate o/mit-repo"
    assert "3. " in out["text"]
    bad = compose.run_cli(seeded, SimpleNamespace(reincarnate_donor=True, repo="x/y",
                                                  into="new:Z"))
    assert "error" in bad and "composition" not in bad


def test_cli_main_compose_offline(seeded, capsys):
    from reincarnate import cli

    seeded.close()
    rc = cli.main(["compose", "dashboard", "health", "--engine", "offline", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["composition"]["status"] == "proposed"


def test_retrieve_without_catalog_module(seeded, monkeypatch):
    import sys

    import reincarnate

    monkeypatch.delattr(reincarnate, "catalog", raising=False)
    monkeypatch.setitem(sys.modules, "reincarnate.catalog", None)
    rows = compose.retrieve(seeded, "alert routing")
    assert rows[0]["name"] == "Alert router"
    assert len(rows) == 5


# -- session engine -----------------------------------------------------------------------
def _session_file(tmp_path, payload, fenced=False):
    path = tmp_path / "proposal.json"
    text = json.dumps(payload)
    path.write_text(f"Proposal:\n```json\n{text}\n```\n" if fenced else text)
    return str(path)


def test_session_proposal_preserved(seeded, tmp_path):
    ids = _ids(seeded)
    path = _session_file(tmp_path, {
        "name": "Pulse Board", "pitch": "one screen for all the boxes",
        "capabilities": [{"capability_id": ids["Status dashboard"], "role": "ui", "order": 2},
                         {"capability_id": ids["Alert router"], "role": "alerts", "order": 1}],
        "glue": "Route alerts onto dashboard tiles.",
        "gaps": [{"need": "auth", "suggested_donors": [], "build_fresh": True}],
    }, fenced=True)
    # the idea alone would not retrieve "Alert router"; ids from the file must be included
    c = compose.propose(seeded, "zzz", engine="session", proposal_path=path)
    p = c.proposal
    assert (c.name, c.pitch, c.status, c.slug) == (
        "Pulse Board", "one screen for all the boxes", "proposed", "pulse-board")
    assert p["engine"] == "session"
    assert [(s["capability_id"], s["role"], s["order"]) for s in p["capabilities"]] == [
        (ids["Alert router"], "alerts", 1), (ids["Status dashboard"], "ui", 2)]
    assert p["glue"] == "Route alerts onto dashboard tiles."
    assert p["gaps"] == [{"need": "auth", "suggested_donors": [], "build_fresh": True}]
    assert p["problems"] == []
    assert [f["level"] for f in p["license_flags"]] == ["deny"]
    assert seeded.get_composition(c.slug).proposal == p
    named = compose.propose(seeded, "zzz", engine="session", proposal_path=path, name="Over")
    assert named.name == "Over"


def test_session_hallucinated_id_dropped(seeded, tmp_path):
    real = _ids(seeded)["Health checks"]
    path = _session_file(tmp_path, {"capabilities": [{"capability_id": 999, "order": 1},
                                                     {"capability_id": real, "order": 2}]})
    c = compose.propose(seeded, "health", engine="session", proposal_path=path)
    assert [s["capability_id"] for s in c.proposal["capabilities"]] == [real]
    assert any("999" in x for x in c.proposal["problems"])
    assert compose._FEWER in c.proposal["problems"]
    assert c.proposal["engine"] == "session" and seeded.get_composition(c.slug)


def test_session_errors(seeded, tmp_path):
    out = compose.run_cli(seeded, _ns(idea=["x"], engine="session"))
    assert out["error"] == "--proposal FILE is required for --engine session"
    out = compose.run_cli(seeded, _ns(idea=["x"], engine="session",
                                      proposal=str(tmp_path / "missing.json")))
    assert "error" in out and "composition" not in out
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert "error" in compose.run_cli(seeded, _ns(idea=["x"], engine="session",
                                                  proposal=str(bad)))
    with pytest.raises(ValueError):
        compose.propose(seeded, "x", engine="session")
    assert seeded.list_compositions() == []


def test_cli_main_session(seeded, tmp_path, capsys):
    from reincarnate import cli

    ids = _ids(seeded)
    path = _session_file(tmp_path, {"name": "From Session", "capabilities": [
        {"capability_id": ids["Health checks"], "role": "core", "order": 1},
        {"capability_id": ids["Config loader"], "role": "config", "order": 2}]})
    seeded.close()
    rc = cli.main(["compose", "idea", "--engine", "session", "--proposal", path,
                   "--name", "CLI Name", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["composition"]["name"] == "CLI Name"
    assert data["composition"]["proposal"]["engine"] == "session"
    assert len(data["composition"]["proposal"]["capabilities"]) == 2


# -- live-run fixes -----------------------------------------------------------------------
def test_offline_caps_at_eight_and_skips_fill(seeded):
    seeded.upsert_dossier("o", "many", "https://github.com/o/many", "m1", _dossier(
        "MIT", [(f"Widget {i:02d}", "widget thing", "medium") for i in range(12)]))
    ids = _ids(seeded)
    rows = compose.retrieve(seeded, "widget", caps=[ids["Alert router"]])
    assert len(rows) == 13  # retrieve still returns everything relevant (< 25)
    c = compose.propose(seeded, "widget", caps=[ids["Alert router"]], engine="offline")
    caps = c.proposal["capabilities"]
    assert len(caps) == 8
    names = {s["name"] for s in caps}
    assert "Alert router" in names  # explicit caps first
    assert names - {"Alert router"} <= {f"Widget {i:02d}" for i in range(12)}
    # one search hit: fill rows only top it up to the 2-capability minimum
    c = compose.propose(seeded, "alert", engine="offline")
    assert len(c.proposal["capabilities"]) == 2
    assert "Alert router" in {s["name"] for s in c.proposal["capabilities"]}


def test_slug_from_name_or_first_six_words(seeded):
    idea = ("homelab dashboard with app tiles health check pings a service map "
            "and incident timeline")
    c = compose.propose(seeded, idea, engine="offline")
    assert c.slug == "homelab-dashboard-with-app-tiles-health"
    c = compose.propose(seeded, idea, engine="offline", name="Pulse Board")
    assert c.slug == "pulse-board"
    long = compose.slugify("observability " * 6)
    assert len(long) <= 48 and not long.endswith("-")
    assert all(w == "observability" for w in long.split("-"))
    assert compose.slugify("a" * 60) == "a" * 48


def test_license_flags_carry_name_and_dedupe(seeded):
    ids = _ids(seeded)
    rows = compose.retrieve(seeded, "zzz", caps=[ids["Uptime probes"], ids["Alert router"]])
    raw = {"capabilities": [{"capability_id": ids["Uptime probes"]},
                            {"capability_id": ids["Uptime probes"]},
                            {"capability_id": ids["Alert router"]}]}
    prop, _ = compose.validate_proposal(seeded, raw, rows, seeded.get_profile(), target=None)
    keys = [(f["capability_id"], f["level"]) for f in prop["license_flags"]]
    assert len(keys) == len(set(keys)) == 2
    assert {f["name"] for f in prop["license_flags"]} == {"Uptime probes", "Alert router"}
    c = compose.reincarnate_donor(seeded, "o/agpl-repo", target=None)
    text = compose.render_text(c)
    assert "o/agpl-repo AGPL-3.0 — Alert router" in text
    assert "o/agpl-repo AGPL-3.0 — Uptime probes" in text

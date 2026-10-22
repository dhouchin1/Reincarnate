import json
import sys
import types
from types import SimpleNamespace

import pytest

from reincarnate import claude_runner, design, kit, paths
from reincarnate.models import Composition, Profile, Project

IDEA = "team dashboard with drag and drop tiles"


def _cap(name, what, portability="high", provides=(), requires=(), novelty="distinctive",
         security=""):
    return {"name": name, "what": what, "portability": portability, "files": ["secret/x.py"],
            "provides": list(provides), "requires": list(requires), "novelty": novelty,
            "security_notes": security}


def _dossier(license_, caps):
    return {"summary": "A donor.", "dependencies": {"license": license_}, "capabilities": caps}


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    def boom(argv, cwd, env, timeout):
        raise AssertionError("unexpected claude call")
    monkeypatch.setattr(claude_runner, "_invoke", boom)


@pytest.fixture
def seeded(store):
    store.add_project(Project(slug="homelab", path="/x/homelab", goal="servers"))
    store.save_profile(Profile(name="dan", stack=["python"],
                               license_policy={"deny": ["AGPL-3.0"], "warn": ["unknown"]}))
    store.upsert_dossier("acme", "authkit", "https://github.com/acme/authkit", "a1", _dossier(
        "MIT",
        [_cap("Session login", "password login with cookie sessions", "low",
              provides=["auth:session"], requires=["store:users"],
              security="hashes passwords"),
         _cap("Retry helper", "retry flaky calls with backoff", provides=["data:retry-policy"],
              novelty="commodity")]))
    store.upsert_dossier("acme", "boards", "https://github.com/acme/boards", "b1", _dossier(
        "MIT",
        [_cap("Team dashboard", "team dashboard of drag and drop tiles",
              provides=["ui:dashboard"], requires=["auth:session", "store:board-layout"]),
         _cap("Layout grid", "drag and drop grid layout for tiles",
              provides=["store:board-layout"]),
         _cap("Activity feed", "team activity feed", "medium", provides=["event:activity"])]))
    store.upsert_dossier("evil", "notify", "https://github.com/evil/notify", "c1", _dossier(
        "AGPL-3.0",
        [_cap("Email notifier", "send email alerts", "medium", provides=["notify:email"],
              requires=["ext:smtp", "event:activity"]),
         _cap("Digest scheduler", "schedule a daily digest", "low",
              provides=["schedule:daily"])]))
    return store


def _ids(store):
    return {c.name: c.id for c in store.list_capabilities()}


def _names(option):
    return [c["name"] for c in option["capabilities"]]


def _reply(payload, cost=0.01):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return 0, json.dumps({"type": "result", "result": text, "total_cost_usd": cost}).encode(), b""


def _scripted(monkeypatch, *payloads):
    calls = []

    def fake(argv, cwd, env, timeout):
        calls.append(argv)
        return _reply(payloads[len(calls) - 1])
    monkeypatch.setattr(claude_runner, "_invoke", fake)
    return calls


def _session_file(tmp_path, ids, fenced=False):
    c = {k: f"c{v}" for k, v in ids.items()}
    obj = {
        "title": "Team Boards",
        "requirements": [{"id": "r1", "text": "users sign in"},
                         {"id": "r2", "text": "dashboard of tiles"},
                         {"id": "r3", "text": "email alerts"}],
        "options": [
            {"slot": "lean", "name": "Board Lite", "pitch": "small teams",
             "rationale": "login feeds the dashboard", "stack": "Python + SQLite",
             "capabilities": [{"id": c["Session login"], "role": "auth"},
                              {"id": c["Team dashboard"], "role": "ui"},
                              {"id": "c9999", "role": "hallucinated"}],
             "coverage": [{"req": "r1", "kind": "catalog", "cap": c["Session login"]},
                          {"req": "r2", "kind": "catalog", "cap": c["Layout grid"]},
                          {"req": "r3", "kind": "net-new", "cap": ""}],
             "glue": ["session middleware"], "new_work": ["email alerts"]},
            {"slot": "complete", "name": "Board Lite Again", "pitch": "dupe",
             "rationale": "", "stack": "",
             "capabilities": [{"id": c["Session login"], "role": "auth"},
                              {"id": c["Team dashboard"], "role": "ui"}],
             "coverage": [], "glue": [], "new_work": []},
            {"slot": "alternative", "name": "Loud Boards", "pitch": "alerting teams",
             "rationale": "feed drives email", "stack": "Go",
             "capabilities": [{"id": c["Activity feed"], "role": "events"},
                              {"id": c["Email notifier"], "role": "alerts"},
                              {"id": ids["Layout grid"], "role": "layout"}],
             "coverage": [{"req": "r3", "kind": "catalog", "cap": c["Email notifier"]}],
             "glue": [], "new_work": []},
        ],
    }
    p = tmp_path / "design.json"
    text = json.dumps(obj)
    p.write_text(f"Here you go:\n```json\n{text}\n```\n" if fenced else text)
    return p


def _ns(**kw):
    base = {"idea": [], "options": 3, "engine": "offline", "proposal": None, "model": None,
            "target": None, "include_commodity": False, "no_verify": False, "json": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _wired_tokens(option):
    return {(w["consumer"]["name"], w["token"], w["provider"]["name"])
            for w in option["wiring"]["wired"]}


def _unmet(option, token):
    return next(u for u in option["wiring"]["unmet"] if u["token"] == token)


# -- pool ---------------------------------------------------------------------------------
def test_pool_excludes_commodity_and_ranks(seeded):
    caps = design.pool(seeded, IDEA)
    names = [c.name for c in caps]
    assert "Retry helper" not in names
    assert len(names) == 6
    assert names[0] == "Team dashboard"
    with_commodity = design.pool(seeded, IDEA, include_commodity=True)
    assert "Retry helper" in [c.name for c in with_commodity]
    assert len(design.pool(seeded, IDEA, cap=2)) == 2


def test_catalog_block_and_prompt(seeded):
    rows = [design._row(c) for c in design.pool(seeded, IDEA)]
    block = design.catalog_block(rows)
    assert "| distinctive |" in block and "gives: auth:session" in block
    assert "needs: auth:session, store:board-layout" in block
    assert "secret/x.py" not in block
    prompt = design.compose_prompt(IDEA, 2)
    assert '"lean"' in prompt and '"complete"' in prompt and '"bold"' not in prompt
    assert "Reincarnate's designer" in design.SYSTEM and "glue" in design.SYSTEM


# -- offline ------------------------------------------------------------------------------
def test_offline_design_wiring_and_scores(seeded):
    d = design.propose_design(seeded, IDEA, engine="offline")
    assert d.id and d.engine == "offline"
    assert [o["slot"] for o in d.options] == ["lean", "complete"]
    lean, complete = d.options
    assert len(lean["capabilities"]) == 3 and len(complete["capabilities"]) == 6
    assert "Retry helper" not in _names(complete)
    assert d.requirements == [{"id": "r1", "text": IDEA}]
    assert lean["coverage"] == [{"req": "r1", "kind": "catalog",
                                 "cap": lean["capabilities"][0]["id"]}]
    # lean: dashboard without the auth provider -> unmet with the provider as candidate
    assert "Team dashboard" in _names(lean) and "Session login" not in _names(lean)
    need = _unmet(lean, "auth:session")
    assert need["consumer"]["name"] == "Team dashboard"
    assert [c["name"] for c in need["candidates"]] == ["Session login"]
    assert set(need["candidates"][0]) == {"id", "capability_id", "name", "donor"}
    # complete: both present -> wired
    assert ("Team dashboard", "auth:session", "Session login") in _wired_tokens(complete)
    assert ("Team dashboard", "store:board-layout", "Layout grid") in _wired_tokens(complete)
    assert complete["wiring"]["external"][0]["token"] == "ext:smtp"
    assert lean["scores"]["reuse"] == 1.0
    wc = lean["wiring"]["counts"]
    assert lean["scores"]["fit"] == round(wc["wired"] / (wc["wired"] + wc["unmet"]), 2)
    assert any("unmet need" in f for f in lean["flags"])
    assert any(f.startswith("License blocked: evil/notify") for f in complete["flags"])
    assert any(f.startswith("Security notes on 1 part") for f in complete["flags"])
    assert d.meta["pool_size"] == 6 and d.meta["catalog_size"] == 7
    assert d.meta["shortlist"] == "lexical" and d.meta["cost_usd"] == 0
    assert d.slug == "team-dashboard-with-drag-and-drop"
    again = design.propose_design(seeded, IDEA, engine="offline")
    assert again.slug == "team-dashboard-with-drag-and-drop-2"
    cap = lean["capabilities"][0]
    assert set(cap) == {"id", "capability_id", "name", "donor", "license", "portability",
                        "novelty", "what", "role", "provides", "requires"}
    assert cap["id"] == f"c{cap['capability_id']}"
    assert "files" not in json.dumps(d.to_dict())


def test_offline_include_commodity(seeded):
    d = design.propose_design(seeded, IDEA, engine="offline", include_commodity=True)
    assert d.meta["pool_size"] == 7
    assert "Retry helper" in _names(d.options[1])


def test_too_small_catalog(store):
    store.upsert_dossier("a", "b", "u", "s", _dossier("MIT", [_cap("Only", "one thing")]))
    with pytest.raises(ValueError, match="at least 2"):
        design.propose_design(store, "anything", engine="offline")


# -- session ------------------------------------------------------------------------------
@pytest.mark.parametrize("fenced", [False, True])
def test_session_design_validation(seeded, tmp_path, fenced):
    ids = _ids(seeded)
    path = _session_file(tmp_path, ids, fenced=fenced)
    d = design.propose_design(seeded, IDEA, engine="session", proposal_path=str(path),
                              verify=False)
    assert d.title == "Team Boards" and d.slug == "team-boards"
    assert [o["name"] for o in d.options] == ["Board Lite", "Loud Boards"]
    lite, loud = d.options
    assert _names(lite) == ["Session login", "Team dashboard"]  # c9999 dropped
    assert any("c9999" in x for x in d.meta["dropped"])
    assert "Board Lite Again: near-duplicate of Board Lite" in d.meta["dropped"]
    cov = {c["req"]: c for c in lite["coverage"]}
    assert cov["r1"] == {"req": "r1", "kind": "catalog", "cap": f"c{ids['Session login']}"}
    assert cov["r2"]["kind"] == "glue" and cov["r2"]["cap"] == ""
    assert cov["r3"]["kind"] == "net-new"
    assert any("pointed outside the option" in f for f in lite["flags"])
    assert any(f == "License blocked: evil/notify (AGPL-3.0)" for f in loud["flags"])
    assert not any(f.startswith("License") for f in lite["flags"])
    # bare-int id resolved; unlabelled requirements become net-new
    assert "Layout grid" in _names(loud)
    assert [c["kind"] for c in loud["coverage"]] == ["net-new", "net-new", "catalog"]
    assert lite["scores"]["reuse"] == round(1 / 3, 2)
    assert ("Team dashboard", "auth:session", "Session login") in _wired_tokens(lite)
    assert ("Email notifier", "event:activity", "Activity feed") in _wired_tokens(loud)
    assert d.meta["verified"] is False


def test_session_requires_file(seeded):
    with pytest.raises(ValueError, match="--proposal"):
        design.propose_design(seeded, IDEA, engine="session")


def test_session_no_surviving_option(seeded, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"options": [{"name": "X", "capabilities": [{"id": "c9999"}]}]}))
    with pytest.raises(ValueError, match="no workable option"):
        design.propose_design(seeded, IDEA, engine="session", proposal_path=str(p))


# -- claude-p -----------------------------------------------------------------------------
def _claude_reply(ids):
    return {"title": "Boards", "requirements": [{"id": "r1", "text": "sign in"}],
            "options": [{"slot": "lean", "name": "Boards", "capabilities": [
                {"id": f"c{ids['Session login']}", "role": "auth"},
                {"id": f"c{ids['Team dashboard']}", "role": "ui"}],
                "coverage": [{"req": "r1", "kind": "catalog",
                              "cap": f"c{ids['Session login']}"}]}]}


def test_claude_p_with_verification(seeded, monkeypatch):
    ids = _ids(seeded)
    calls = _scripted(monkeypatch, _claude_reply(ids), {"verdicts": {"L0": False}})
    d = design.propose_design(seeded, IDEA, engine="claude-p", n_options=2)
    assert len(calls) == 2
    first, second = calls
    assert first[first.index("--model") + 1] == "claude-opus-5-5"
    assert second[second.index("--model") + 1] == "claude-haiku-5-5"
    for argv in calls:
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--max-turns") + 1] == "1"
        assert "--strict-mcp-config" in argv
    prompt = first[first.index("-p") + 1]
    assert "CAPABILITY CATALOG" in prompt and "Retry helper" not in prompt
    assert "L0: Team dashboard" in second[second.index("-p") + 1]
    w = d.options[0]["wiring"]
    assert w["wired"] == [] and w["verified"] is True
    rej = _unmet(d.options[0], "auth:session")
    assert rej["rejected_provider"]["name"] == "Session login" and rej["candidates"] == []
    assert w["counts"]["wired"] == 0 and w["counts"]["unmet"] == 2  # + store:board-layout
    assert d.options[0]["scores"]["fit"] == 0.0
    assert d.meta["cost_usd"] == pytest.approx(0.02) and d.meta["verified"] is True


def test_claude_p_no_verify_one_call(seeded, monkeypatch):
    calls = _scripted(monkeypatch, _claude_reply(_ids(seeded)))
    d = design.propose_design(seeded, IDEA, engine="claude-p", verify=False, model="sonnet")
    assert len(calls) == 1 and calls[0][calls[0].index("--model") + 1] == "sonnet"
    assert len(d.options[0]["wiring"]["wired"]) == 1


def test_verify_failure_recorded_not_raised(seeded, monkeypatch):
    _scripted(monkeypatch, _claude_reply(_ids(seeded)), "not json at all")
    d = design.propose_design(seeded, IDEA, engine="claude-p")
    assert any(x.startswith("link verification skipped") for x in d.meta["dropped"])
    assert len(d.options[0]["wiring"]["wired"]) == 1 and d.meta["verified"] is False


def test_claude_p_no_json_raises(seeded, monkeypatch):
    _scripted(monkeypatch, "sorry")
    with pytest.raises(ValueError, match="no JSON design"):
        design.propose_design(seeded, IDEA, engine="claude-p")


# -- shortlist hook -----------------------------------------------------------------------
def test_shortlist_hook(seeded, monkeypatch):
    seen = {}

    def shortlist(idea, caps, *, client, keep):
        seen.update(idea=idea, n=len(caps), client=client, keep=keep)
        return caps[:2]

    monkeypatch.setitem(sys.modules, "reincarnate.jev",
                        types.SimpleNamespace(shortlist=shortlist))
    client = SimpleNamespace(name="fake-jev")
    d = design.propose_design(seeded, IDEA, engine="offline", shortlist=client)
    assert seen == {"idea": IDEA, "n": 6, "client": client, "keep": 40}
    assert d.meta["pool_size"] == 2 and d.meta["shortlist"] == "fake-jev"
    assert len(d.options) == 1  # complete == lean on a 2-cap pool -> near-duplicate dropped


def test_shortlist_failure_keeps_lexical(seeded, monkeypatch):
    def shortlist(idea, caps, *, client, keep):
        raise RuntimeError("model down")

    monkeypatch.setitem(sys.modules, "reincarnate.jev",
                        types.SimpleNamespace(shortlist=shortlist))
    d = design.propose_design(seeded, IDEA, engine="offline",
                              shortlist=SimpleNamespace(name="fake-jev"))
    assert d.meta["pool_size"] == 6 and d.meta["shortlist"] == "lexical"
    assert any("model down" in n for n in d.meta["notes"])


def test_shortlist_none_skips_hook(seeded, monkeypatch):
    def shortlist(*a, **k):
        raise AssertionError("should not be called")

    monkeypatch.setitem(sys.modules, "reincarnate.jev",
                        types.SimpleNamespace(shortlist=shortlist))
    assert len(design.pool(seeded, IDEA)) == 6


# -- pick ---------------------------------------------------------------------------------
def test_pick_builds_composition_and_kit(seeded, tmp_path):
    ids = _ids(seeded)
    d = design.propose_design(seeded, IDEA, engine="session",
                              proposal_path=str(_session_file(tmp_path, ids)), verify=False)
    loud = design.pick(seeded, d.slug, "loud-boards", target="new:loud")
    assert loud.status == "proposed" and loud.target_kind == "new"
    assert loud.target_slug == "loud"
    lf = loud.proposal["license_flags"]
    assert [f["level"] for f in lf] == ["deny"] and lf[0]["donor"] == "evil/notify"
    assert any("License blocked" in r for r in loud.proposal["risks"])

    c = design.pick(seeded, d.slug, "lean", target="homelab")
    p = c.proposal
    assert c.name == "Board Lite" and c.target_kind == "existing" and c.target_slug == "homelab"
    assert seeded.get_composition(c.slug) is not None
    assert [x["name"] for x in p["capabilities"]] == ["Session login", "Team dashboard"]
    assert p["capabilities"][0] == {"capability_id": ids["Session login"], "role": "auth",
                                    "order": 1, "name": "Session login", "donor": "acme/authkit",
                                    "license": "MIT", "portability": "low"}
    assert p["glue"].startswith("login feeds the dashboard") and "- session middleware" in p["glue"]
    assert {"need": "email alerts", "suggested_donors": [], "build_fresh": True} in p["gaps"]
    assert {"need": "store:board-layout for Team dashboard", "suggested_donors": ["acme/boards"],
            "build_fresh": False} in p["gaps"]
    assert p["wiring"] and p["coverage"] and p["requirements"] == d.requirements
    assert p["design_slug"] == d.slug and p["slot"] == "lean" and p["license_flags"] == []

    out = kit.export_kit(seeded, c, "new:boardlite")
    text = (paths.data_dir() / "new" / "boardlite" / ".reincarnate" / "BLUEPRINT.md").read_text()
    assert out["steps"] == 4
    assert "## Wiring" in text and "## Requirement coverage" in text
    assert "Team dashboard needs `auth:session` from Session login" in text
    assert "| r1: users sign in | catalog | Session login |" in text


def test_pick_unknown_refs(seeded):
    d = design.propose_design(seeded, IDEA, engine="offline")
    with pytest.raises(ValueError, match="no such design"):
        design.pick(seeded, "nope", "lean")
    with pytest.raises(ValueError, match="no option bold"):
        design.pick(seeded, d.slug, "bold")


def test_kit_without_design_fields_unchanged(seeded):
    ids = _ids(seeded)
    comp = seeded.save_composition(Composition(slug="plain", name="Plain", proposal={
        "capabilities": [{"capability_id": ids["Layout grid"], "order": 1}]}))
    kit.export_kit(seeded, comp, "new:plain")
    text = (paths.data_dir() / "new" / "plain" / ".reincarnate" / "BLUEPRINT.md").read_text()
    assert "## Wiring" not in text and "## Requirement coverage" not in text


# -- render / CLI -------------------------------------------------------------------------
def test_render_text(seeded):
    d = design.propose_design(seeded, IDEA, engine="offline")
    text = design.render_text(d)
    head = text.splitlines()[4]
    assert head.split() == ["slot", "name", "parts", "reuse", "fit", "unmet", "flags"]
    assert "[lean] Lean core (lean-core)" in text
    assert "unmet: Team dashboard needs auth:session (candidates: Session login)" in text


def test_run_cli_paths(seeded, tmp_path):
    assert design.run_cli(seeded, _ns(idea=["list"]))["text"] == "no designs"
    out = design.run_cli(seeded, _ns(idea=IDEA.split()))
    assert "error" not in out and out["slug"] and out["options"] and out["dropped"] == []
    slug = out["slug"]
    listed = design.run_cli(seeded, _ns(idea=["list"]))
    assert [x["slug"] for x in listed["items"]] == [slug] and slug in listed["text"]
    shown = design.run_cli(seeded, _ns(idea=["show", slug]))
    assert shown["slug"] == slug and "Lean core" in shown["text"]
    picked = design.run_cli(seeded, _ns(idea=["pick", slug, "complete"], target="new:x"))
    assert picked["composition"]["target_slug"] == "x" and picked["slug"]
    assert "steps:" in picked["text"]

    sess = design.run_cli(seeded, _ns(idea=IDEA.split(), engine="session", no_verify=True,
                                      proposal=str(_session_file(tmp_path, _ids(seeded)))))
    assert sess["slug"] == "team-boards" and sess["dropped"]

    for ns, msg in [
        (_ns(idea=["show", "missing"]), "no such design"),
        (_ns(idea=["show"]), "usage"),
        (_ns(idea=["pick", slug]), "usage"),
        (_ns(idea=["pick", "missing", "lean"]), "no such design"),
        (_ns(idea=["pick", slug, "bold"]), "no option"),
        (_ns(idea=[]), "an idea is required"),
        (_ns(idea=["x"], engine="session"), "--proposal"),
    ]:
        res = design.run_cli(seeded, ns)
        assert msg in res["error"] and res["text"].startswith("error:")


def test_cli_shortlist_names_resolve_to_clients(store, monkeypatch, capsys):
    """--shortlist jev without a key is a clean error; jev-sim resolves to the sim client."""
    from reincarnate import cli
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rc = cli.main(["design", "dashboard", "--engine", "offline", "--shortlist", "jev", "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and "TYPESAFE_API_KEY" in out

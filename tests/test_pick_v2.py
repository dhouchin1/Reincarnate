"""Batch 8: design and pick. Auto-fill sees every provider, pick recomputes after the fill,
shared gaps merge, commodity parts stay out, targets are checked, rejected links keep their
alternatives, and requirements without parts still produce steps."""
import json
from types import SimpleNamespace

import pytest

from reincarnate import claude_runner, cli, compose, design, kit, paths, salvage
from reincarnate.models import Profile, Project


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    def boom(argv, cwd, env, timeout, stdin):
        raise AssertionError("unexpected claude call")
    monkeypatch.setattr(claude_runner, "_invoke", boom)


def _cap(name, what, portability="high", provides=(), requires=(), novelty="distinctive"):
    return {"name": name, "what": what, "portability": portability, "files": ["x.py"],
            "provides": list(provides), "requires": list(requires), "novelty": novelty,
            "security_notes": ""}


def _seed(store, donors):
    """donors: {"owner/repo": (license, [caps])}. Returns {name: id}."""
    store.save_profile(Profile(name="dan", stack=["python"],
                               license_policy={"deny": ["AGPL-3.0"], "warn": ["unknown"]}))
    store.add_project(Project(slug="homelab", path="/x/homelab", goal="servers"))
    store.add_project(Project(slug="notes", path="/x/notes", goal="notes"))
    for donor, (lic, caps) in donors.items():
        owner, repo = donor.split("/")
        store.upsert_dossier(owner, repo, f"https://github.com/{donor}", "s", {
            "summary": "A donor.", "dependencies": {"license": lic}, "capabilities": caps})
    return {c.name: c.id for c in store.list_capabilities()}


def _design(store, ids, names, *, requirements=None, coverage=None, glue=None, new_work=None,
            stack="Python", verify=False, include_commodity=False, pins=None, tmp=None):
    path = (tmp or paths.data_dir()) / "d.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "title": "T", "requirements": requirements if requirements is not None
        else [{"id": "r1", "text": "read the feed"}],
        "options": [{"slot": "lean", "name": "Reader", "stack": stack,
                     "capabilities": [{"id": f"c{ids[n]}"} for n in names],
                     "coverage": coverage if coverage is not None
                     else [{"req": "r1", "kind": "catalog", "cap": f"c{ids[names[0]]}"}],
                     "glue": glue if glue is not None else [], "new_work": new_work or []}]}))
    return design.propose_design(store, "feed reader", engine="session", proposal_path=str(path),
                                 verify=verify, include_commodity=include_commodity, pins=pins)


def _pick(store, d, target="new:app"):
    return design.pick(store, d.slug, "lean", target=target)


def _filler():
    return _cap("Filler", "unrelated filler part", provides=["ui:filler"])


def _gap(proposal, token):
    return next(g for g in proposal["gaps"] if g.get("token") == token)


# 1 -- same-donor auto-fill survives a long provider list ------------------------------------
def test_same_donor_provider_beats_many_better_ported_strangers(store):
    donors = {"me/reader": ("MIT", [_cap("News reader", "reads rss news items", "high",
                                         requires=["data:feed"], provides=["ui:reader"]),
                                    _cap("Odd helper", "misc plumbing", "low",
                                         provides=["data:feed"]), _filler()])}
    for i in range(5):
        donors[f"o{i}/feeds"] = ("MIT", [_cap(f"Stranger {i}", "unrelated thing", "high",
                                              provides=["data:feed"])])
    ids = _seed(store, donors)
    d = _design(store, ids, ["News reader", "Filler"])
    unmet = d.options[0]["wiring"]["unmet"][0]
    assert len(unmet["candidates"]) == 6  # nothing cut before ranking
    p = _pick(store, d).proposal
    assert [(a["name"], a["reason"]) for a in p["auto_filled"]] == [("Odd helper", "same donor")]
    assert "Odd helper" in [c["name"] for c in p["capabilities"]]
    # the displayed list is still cut
    text = design.render_text(d)
    assert text.count("Stranger") <= design.MAX_CITED


# 2 -- pick recomputes risks, scores, coverage after auto-fill -------------------------------
def test_pick_recomputes_flags_scores_and_coverage_after_fill(store):
    ids = _seed(store, {
        "me/reader": ("MIT", [_cap("News reader", "reads rss news items", requires=["data:feed"],
                                   provides=["ui:reader"]), _filler()]),
        "me/other": ("unknown", [_cap("Feed box", "stores the feed", provides=["data:feed"])])})
    ids_by = ids
    d = _design(store, ids_by, ["News reader", "Filler"],
                requirements=[{"id": "r1", "text": "show a reader"},
                              {"id": "r2", "text": "keep items in the Feed box"}],
                coverage=[{"req": "r1", "kind": "catalog", "cap": f"c{ids['News reader']}"},
                          {"req": "r2", "kind": "glue", "cap": ""}])
    opt = d.options[0]
    assert any("1 unmet need(s)" in f for f in opt["flags"]) and opt["scores"]["fit"] == 0.0
    c = _pick(store, d)
    p = c.proposal
    assert [a["name"] for a in p["auto_filled"]] == ["Feed box"]
    assert not any("unmet" in r for r in p["risks"])
    assert any(r.startswith("License review: me/other") for r in p["risks"])
    assert p["scores"] == {"reuse": 1.0, "fit": 1.0}
    assert p["wiring"]["counts"]["unmet"] == 0 and p["wiring"]["counts"]["wired"] == 1
    cov = {r["req"]: r for r in p["coverage"]}
    assert cov["r2"]["kind"] == "catalog" and cov["r2"]["cap"] == f"c{ids['Feed box']}"
    # no glue step is exported for the requirement the fill now covers
    kit.export_kit(store, c, "new:app")
    specs = sorted(f.name for f in (paths.data_dir() / "new" / "app" / ".reincarnate"
                                    / "specs").iterdir())
    assert not any("glue" in s for s in specs)
    # the stored composition carries the same values
    stored = store.get_composition(c.slug).proposal
    assert stored["risks"] == p["risks"] and stored["scores"] == p["scores"]


# 3 -- one gap per token, before its first consumer, naming the rest -------------------------
def test_two_consumers_of_one_missing_token_get_one_build_fresh_step(store):
    ids = _seed(store, {
        "a/one": ("MIT", [_cap("Alpha", "alpha part", requires=["data:feed"],
                               provides=["ui:alpha"])]),
        "b/two": ("MIT", [_cap("Beta", "beta part", requires=["data:feed"],
                               provides=["ui:beta"])]),
        "z/far": ("MIT", [_cap("Zed", "zzz qqq", provides=["data:feed"])])})
    d = _design(store, ids, ["Alpha", "Beta"])
    c = _pick(store, d)
    p = c.proposal
    gaps = [g for g in p["gaps"] if g.get("token") == "data:feed"]
    assert len(gaps) == 1
    assert gaps[0]["before_capability_id"] == ids["Alpha"]
    assert gaps[0]["also_consumers"] == [{"capability_id": ids["Beta"], "name": "Beta"}]
    kit.export_kit(store, c, "new:app")
    specs = paths.data_dir() / "new" / "app" / ".reincarnate" / "specs"
    fresh = sorted(f.name for f in specs.glob("*fresh*"))
    assert len(fresh) == 1
    assert fresh[0].startswith("01-")  # right before the first consumer (step 2)
    text = (specs / fresh[0]).read_text()
    assert "**Also used by:** Beta (step 3)" in text
    assert "data:feed from the build-fresh step (step 1)" in (specs / "03-beta.md").read_text()
    assert "Alpha (step 2)" in text  # the first consumer is named in the spec body
    # the batch-5 part-identity checks still hold: every row carries name and donor
    assert all(r["name"] and r["donor"] for r in store.get_composition(c.slug)
               .proposal["capabilities"])


# 4 -- commodity parts ------------------------------------------------------------------------
def _commodity_donors():
    return {"me/reader": ("MIT", [_cap("News reader", "reads rss news items",
                                       requires=["data:feed"], provides=["ui:reader"]),
                                  _cap("Commodity box", "retry helper", provides=["data:feed"],
                                       novelty="commodity"), _filler()])}


def test_commodity_candidates_never_autofill_unless_the_design_allows_them(store):
    ids = _seed(store, _commodity_donors())
    d = _design(store, ids, ["News reader", "Filler"])
    p = _pick(store, d).proposal
    assert p["auto_filled"] == [] and "Commodity box" not in [c["name"] for c in p["capabilities"]]
    assert _gap(p, "data:feed")["candidates"][0]["name"] == "Commodity box"  # still cited
    d2 = _design(store, ids, ["News reader", "Filler"], include_commodity=True)
    assert d2.meta["include_commodity"] is True
    p2 = _pick(store, d2).proposal
    assert [a["name"] for a in p2["auto_filled"]] == ["Commodity box"]


def test_commodity_ranks_last_when_allowed(store):
    donors = _commodity_donors()
    donors["o/feeds"] = ("MIT", [_cap("Feed parser", "parses rss news items into entries",
                                      provides=["data:feed"])])
    ids = _seed(store, donors)
    d = _design(store, ids, ["News reader", "Filler"], include_commodity=True)
    ctx = design._wiring_context(store)
    need = d.options[0]["wiring"]["unmet"][0]
    ranked = design.rank_candidates(ctx, need, store.get_profile())
    # the commodity part is the consumer's own donor, yet it still ranks last
    assert [c["name"] for c in ranked] == ["Feed parser", "Commodity box"]
    assert ranked[-1]["same_donor"] and ranked[-1]["commodity"]


def test_pinned_commodity_part_survives_pick(store):
    ids = _seed(store, _commodity_donors())
    d = _design(store, ids, ["News reader", "Filler"], pins=[ids["Commodity box"]])
    assert any(c.get("pinned") for c in d.options[0]["capabilities"])
    p = _pick(store, d).proposal
    assert "Commodity box" in [c["name"] for c in p["capabilities"]]
    assert p["auto_filled"] == []  # it is already in the option; nothing else is pulled in


# 5 -- targets ---------------------------------------------------------------------------------
def test_salvage_into_must_be_a_registered_project_or_new_name(store, capsys):
    ids = _seed(store, {"me/reader": ("MIT", [_cap("News reader", "reads", provides=["ui:r"])])})
    cid = ids["News reader"]
    for into in ("typo", "", "/tmp/somewhere", "../x", "new:", "new:   "):
        out = salvage.run_cli(store, SimpleNamespace(capability_id=cid, into=into))
        assert "error" in out and "spec" not in out
    out = salvage.run_cli(store, SimpleNamespace(capability_id=cid, into="typo"))
    assert "homelab" in out["error"] and "notes" in out["error"]
    assert "spec" in salvage.run_cli(store, SimpleNamespace(capability_id=cid, into="homelab"))
    assert "spec" in salvage.run_cli(store, SimpleNamespace(capability_id=cid, into="new:z"))
    assert cli.main(["salvage", str(cid), "--into", "typo", "--json"]) == 1
    assert "homelab" in json.loads(capsys.readouterr().out)["error"]


def test_compose_target_typo_is_an_error_not_a_problem(store, capsys):
    _seed(store, {"me/reader": ("MIT", [_cap("A", "dashboard one", provides=["ui:a"]),
                                        _cap("B", "dashboard two", provides=["ui:b"])])})
    with pytest.raises(ValueError, match="homelab"):
        compose.propose(store, "dashboard", engine="offline", target="typo")
    with pytest.raises(ValueError, match="needs a name"):
        compose.propose(store, "dashboard", engine="offline", target="new:")
    assert store.list_compositions() == []
    out = compose.run_cli(store, SimpleNamespace(idea=["dashboard"], engine="offline",
                                                 target="typo", caps=None, name=None,
                                                 proposal=None))
    assert "homelab" in out["error"]
    assert cli.main(["compose", "dashboard", "--engine", "offline", "--target", "typo",
                     "--json"]) == 1
    capsys.readouterr()
    c = compose.propose(store, "dashboard", engine="offline", target="homelab")
    assert (c.target_kind, c.target_slug) == ("existing", "homelab")


# 6 -- a rejected link keeps its alternatives --------------------------------------------------
def test_rejected_link_lists_other_providers_and_names_the_rejected_one(store, monkeypatch):
    ids = _seed(store, {
        "me/reader": ("MIT", [_cap("News reader", "reads rss news items",
                                   requires=["data:feed"], provides=["ui:reader"]), _filler()]),
        "p/one": ("MIT", [_cap("Provider one", "feeds one", provides=["data:feed"])]),
        "p/two": ("MIT", [_cap("Provider two", "feeds two", "medium", provides=["data:feed"])])})
    reply = json.dumps({"type": "result", "result": json.dumps({"verdicts": {"L0": False}}),
                        "total_cost_usd": 0.01}).encode()
    monkeypatch.setattr(claude_runner, "_invoke", lambda *a, **k: (0, reply, b""))
    d = _design(store, ids, ["News reader", "Provider one", "Filler"], verify=True)
    unmet = d.options[0]["wiring"]["unmet"][0]
    assert unmet["rejected_provider"]["name"] == "Provider one"
    assert [c["name"] for c in unmet["candidates"]] == ["Provider two"]
    c = _pick(store, d)
    gap = _gap(c.proposal, "data:feed")
    assert gap["rejected_by_verifier"] == "Provider one"
    assert [x["name"] for x in gap["candidates"]] == ["Provider two"]
    assert c.proposal["auto_filled"] == []  # a rejected need is never auto-filled
    kit.export_kit(store, c, "new:app")
    spec = next((paths.data_dir() / "new" / "app" / ".reincarnate" / "specs").glob(
        "*fresh*")).read_text()
    assert "the link verifier rejected Provider one for this need; check before building fresh" \
        in spec
    assert "- Provider two (p/two" in spec


# 7 -- requirements without parts --------------------------------------------------------------
def test_net_new_requirement_without_new_work_becomes_a_build_fresh_step(store):
    ids = _seed(store, {"me/reader": ("MIT", [_cap("News reader", "reads", provides=["ui:r"]),
                                              _filler()])})
    reqs = [{"id": "r1", "text": "read the feed"}, {"id": "r2", "text": "export a weekly pdf"},
            {"id": "r3", "text": "audit every login"}]
    cov = [{"req": "r1", "kind": "catalog", "cap": f"c{ids['News reader']}"},
           {"req": "r2", "kind": "net-new", "cap": ""}]  # r3 has no label
    d = _design(store, ids, ["News reader", "Filler"], requirements=reqs, coverage=cov,
                new_work=["audit every login trail"])
    c = _pick(store, d)
    needs = [g["need"] for g in c.proposal["gaps"]]
    assert "Build fresh for requirement r2: export a weekly pdf" in needs
    assert not any(n.startswith("Build fresh for requirement r3") for n in needs)  # new_work
    assert "audit every login trail" in needs
    assert c.proposal["problems"] == [
        "requirement r3 had no coverage label; treated as net-new"]
    kit.export_kit(store, c, "new:app")
    specs = [f.name for f in (paths.data_dir() / "new" / "app" / ".reincarnate"
                              / "specs").iterdir()]
    assert any("requirement-r2" in s for s in specs)


def test_unlabelled_requirement_without_new_work_also_gets_a_step(store):
    ids = _seed(store, {"me/reader": ("MIT", [_cap("News reader", "reads", provides=["ui:r"]),
                                              _filler()])})
    d = _design(store, ids, ["News reader", "Filler"], coverage=[])
    p = _pick(store, d).proposal
    assert [g["need"] for g in p["gaps"]] == ["Build fresh for requirement r1: read the feed"]
    assert p["problems"] == ["requirement r1 had no coverage label; treated as net-new"]


# 8 -- compose retrieval -----------------------------------------------------------------------
def test_compose_query_keeps_short_meaningful_words_and_drops_filler():
    assert compose._idea_query("Add a UI for the DB of our app") == "add ui db"
    assert compose._idea_query("the and of") == ""


def test_compose_retrieval_finds_a_short_word_capability(store):
    _seed(store, {"me/ui": ("MIT", [_cap("UI kit", "ui widgets", provides=["ui:kit"]),
                                    _cap("Other", "zebra stripes", provides=["ui:z"])])})
    rows = compose.retrieve(store, "a UI")
    assert rows[0]["name"] == "UI kit" and rows[0]["retrieved_by"] == "search"


# edge cases -----------------------------------------------------------------------------------
def test_denied_only_provider_becomes_build_fresh_never_the_denied_part(store):
    ids = _seed(store, {
        "me/reader": ("MIT", [_cap("News reader", "reads rss news items",
                                   requires=["data:feed"], provides=["ui:reader"]), _filler()]),
        "bad/feeds": ("AGPL-3.0", [_cap("Feed parser", "parses rss news items",
                                        provides=["data:feed"])])})
    p = _pick(store, _design(store, ids, ["News reader", "Filler"])).proposal
    assert "Feed parser" not in [c["name"] for c in p["capabilities"]]
    assert _gap(p, "data:feed")["build_fresh"] is True


def test_option_with_no_wiring_picks_cleanly(store):
    ids = _seed(store, {"me/a": ("MIT", [_cap("One", "first thing", provides=["ui:one"]),
                                         _cap("Two", "second thing", provides=["ui:two"])])})
    c = _pick(store, _design(store, ids, ["One", "Two"]))
    p = c.proposal
    assert p["auto_filled"] == [] and p["gaps"] == [] and p["wiring"]["counts"]["unmet"] == 0
    assert p["scores"]["fit"] is None and p["scores"]["reuse"] == 1.0
    kit.export_kit(store, c, "new:app")


def test_session_design_with_list_stack_and_string_glue_reads_sanely(store):
    ids = _seed(store, {"me/a": ("MIT", [_cap("One", "first thing", provides=["ui:one"]),
                                         _cap("Two", "second thing", provides=["ui:two"])])})
    d = _design(store, ids, ["One", "Two"], stack=["Python", "FastAPI"], glue="one string")
    opt = d.options[0]
    assert opt["stack"] == "Python, FastAPI" and opt["glue"] == ["one string"]
    c = _pick(store, d)
    assert c.proposal["stack"] == "Python, FastAPI"
    assert "- one string" in c.proposal["glue"]
    kit.export_kit(store, c, "new:app")
    kd = paths.data_dir() / "new" / "app" / ".reincarnate"
    assert "['" not in (kd / "BLUEPRINT.md").read_text()


def test_an_option_beyond_the_count_is_reported_not_silently_dropped():
    from reincarnate import design
    by_cid = {f"c{i}": {"cid": f"c{i}", "capability_id": i, "name": f"P{i}", "donor": "o/r",
                        "license": "MIT", "portability": "high", "novelty": "pattern",
                        "what": "x", "provides": [], "requires": []} for i in range(1, 9)}
    opts = [{"slot": s, "name": n, "capabilities": [{"id": a}, {"id": b}]}
            for s, n, a, b in (("lean", "L", "c1", "c2"), ("complete", "C", "c3", "c4"),
                               ("alternative", "A", "c5", "c6"), ("bold", "B", "c7", "c8"))]
    dropped = []
    out, _ = design.build_options({"options": opts, "requirements": []}, by_cid, 3, dropped, None)
    assert [o["name"] for o in out] == ["L", "C", "A"]
    assert dropped == ["B: beyond the 3 options asked for (--options 4 keeps it)"]

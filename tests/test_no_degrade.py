"""Batch 2b: a lower tier never overwrites a higher one, and model replies are validated."""
import json
from types import SimpleNamespace

import pytest

from conftest import make_dossier
from reincarnate import claude_runner, enrich, overlay, review
from reincarnate.importers import interfaces as imp
from reincarnate.models import Profile, Project
from test_enrich import FixedClient, _fake, _seed

FIX = "capability-interfaces.json"


def _run(monkeypatch, reply, prompts=None):
    monkeypatch.setattr(claude_runner, "_invoke", _fake([json.dumps(reply)], prompts))


# -- overlay ----------------------------------------------------------------------------------
@pytest.fixture
def ov(store):
    store.save_profile(Profile(name="d", stack=["python"], intents=[]))
    store.add_project(Project(slug="homelab-console", stack=["python"],
                              goal="dashboard for health checks of services"))
    d = store.upsert_dossier("o", "r", "u", "s", make_dossier(
        ("health-check pinger", "pings services and reports health checks")))
    cid = store.list_capabilities()[0].id
    return store, d, cid


def _heur(store, d):
    return overlay.apply(store, d, store.get_profile(), store.list_projects())


def _claude(monkeypatch, store, d):
    arr = [{"capability": "health-check pinger", "project": "homelab-console", "score": 0.7,
            "how": "model"}]
    res = SimpleNamespace(failed=False, text=json.dumps(arr), cost_usd=0.0)
    monkeypatch.setattr(claude_runner, "run", lambda *a, **k: res)
    return overlay.apply(store, d, store.get_profile(), store.list_projects(), engine="claude-p")


@pytest.mark.parametrize("source", ["dossier", "manual", "overlay-claude"])
def test_heuristic_overlay_keeps_stronger_fit(ov, source):
    store, d, cid = ov
    store.set_fit(cid, "homelab-console", 0.8, "stronger", source)
    r = _heur(store, d)
    assert r["fits_written"] == 0 and r["fits_kept"] == 1 and "kept" in r["message"]
    (f,) = store.list_fits()
    assert (f.score, f.source, f.how) == (0.8, source, "stronger")


def test_heuristic_refreshes_heuristic_and_claude_overwrites_it(ov, monkeypatch):
    store, d, cid = ov
    assert _heur(store, d)["fits_written"] == 1
    assert _heur(store, d)["fits_written"] == 1  # equal rank may refresh
    r = _claude(monkeypatch, store, d)
    assert r["fits_written"] == 1 and r["fits_kept"] == 0
    (f,) = store.list_fits()
    assert (f.score, f.source) == (0.7, "overlay-claude")


def test_claude_overlay_does_not_overwrite_dossier_or_manual(ov, monkeypatch):
    store, d, cid = ov
    store.set_fit(cid, "homelab-console", 0.8, "x", "dossier")
    assert _claude(monkeypatch, store, d)["fits_kept"] == 1
    assert store.list_fits()[0].source == "dossier"


def test_run_cli_totals_kept(ov):
    store, d, cid = ov
    store.set_fit(cid, "homelab-console", 0.8, "x", "manual")
    r = overlay.run_cli(store, SimpleNamespace(repo="o/r", all=False, engine="heuristic"))
    assert r["fits_kept"] == 1 and r["total_fits"] == 0


# -- validation -------------------------------------------------------------------------------
def test_validate_reply_novelty_forms(store):
    v = store.get_vocabulary()
    assert enrich.validate_reply({"novelty": " Distinctive. "}, ("novelty",), v) == (
        {"novelty": "distinctive"}, [])
    for bad in ("unknown", "n/a", 123, None, ""):
        assert enrich.validate_reply({"novelty": bad}, ("novelty",), v) == ({}, ["novelty"])


def test_validate_reply_interfaces_acceptance(store):
    v = store.get_vocabulary()
    ok, bad = enrich.validate_reply({"provides": [], "requires": ["ext:docker"]},
                                    ("interfaces",), v)
    assert ok == {"provides": [], "requires": ["ext:docker"]} and bad == []
    ok, bad = enrich.validate_reply({"provides": ["garbage", "??"], "requires": [1]},
                                    ("interfaces",), v)
    assert ok == {} and bad == ["provides", "requires"]
    ok, bad = enrich.validate_reply({"provides": [], "requires": []}, ("interfaces",), v)
    assert bad == [] and ok == {"provides": [], "requires": []}
    for acc in ([], [""], ["a", 3], "text", None):
        assert enrich.validate_reply({"acceptance": acc}, ("acceptance",), v) == (
            {}, ["acceptance"])
    assert enrich.validate_reply({"acceptance": [" x "]}, ("acceptance",), v)[0] == {
        "acceptance": ["x"]}


def test_enrich_rejected_novelty_keeps_old_and_flags(store, monkeypatch):
    ids = _seed(store, 2)
    store.set_capability_fields(ids[0], novelty="pattern", confidence=0.3, needs_review=True)
    _run(monkeypatch, {str(ids[0]): {"novelty": "unknown", "provides": ["auth:session"],
                                     "requires": []},
                       str(ids[1]): {"novelty": "Distinctive.", "provides": [], "requires": []}})
    r = enrich.enrich(store, all_=True)
    a, b = store.get_capability(ids[0]), store.get_capability(ids[1])
    assert a.novelty == "pattern" and a.provides == ["auth:session"]
    assert (a.confidence, a.needs_review) == (0.3, True)  # untouched
    assert b.novelty == "distinctive" and b.provides == [] and b.confidence is None
    assert r["updated"] == 2 and r["rejected_count"] == 1
    assert r["rejected"] == [{"capability_id": ids[0], "fields": ["novelty"],
                              "reason": "missing or invalid: novelty"}]
    assert "1 with rejected fields" in r["text"]


def test_enrich_zero_accepted_is_failed_batch(store, monkeypatch):
    ids = _seed(store, 1)
    _run(monkeypatch, {str(ids[0]): {"novelty": "n/a", "provides": ["junk"], "requires": ["x"]}})
    r = enrich.enrich(store, all_=True)
    assert r["updated"] == 0 and len(r["failed_batches"]) == 1
    assert r["rejected"][0]["fields"] == ["novelty", "provides", "requires"]
    assert store.get_capability(ids[0]).interfaces_rated_at == ""


def test_enrich_acceptance_empty_list_never_written(store, monkeypatch):
    ids = _seed(store, 1)
    _run(monkeypatch, {str(ids[0]): {"acceptance": []}})
    r = enrich.enrich(store, all_=True, fields=("acceptance",))
    assert r["updated"] == 0 and r["failed_batches"]


def test_review_confidence_only_for_validated(store, monkeypatch):
    ids = _seed(store, 2)
    for i in ids:
        store.set_capability_fields(i, confidence=0.2, needs_review=True)
    reply = {str(ids[0]): {"novelty": "pattern", "provides": ["auth:session"], "requires": []},
             str(ids[1]): {"novelty": "dunno", "provides": ["auth:session"], "requires": []}}
    monkeypatch.setattr(claude_runner, "_invoke", _review_fake(reply))
    r = review.review(store)
    a, b = store.get_capability(ids[0]), store.get_capability(ids[1])
    assert (a.confidence, a.needs_review, a.novelty) == (1.0, False, "pattern")
    assert (b.confidence, b.needs_review, b.novelty) == (0.2, True, "")
    assert b.provides == ["auth:session"]  # the valid field is still written
    assert r["reviewed"] == 1 and r["partial"] == 1 and r["rejected_count"] == 1
    assert r["rejected"][0]["capability_id"] == ids[1]


def _review_fake(reply):
    def invoke(argv, cwd, env, timeout, stdin):
        return 0, json.dumps({"result": json.dumps(reply), "total_cost_usd": 0}).encode(), b""
    return invoke


def test_review_zero_accepted_is_failed_batch(store, monkeypatch):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], confidence=0.2, needs_review=True)
    monkeypatch.setattr(claude_runner, "_invoke",
                        _review_fake({str(ids[0]): {"novelty": "x", "provides": 1}}))
    r = review.review(store)
    assert r["reviewed"] == 0 and len(r["failed_batches"]) == 1
    assert store.get_capability(ids[0]).needs_review is True


# -- tier locks -------------------------------------------------------------------------------
def test_reviewed_cap_skipped_and_not_sent(store, monkeypatch):
    ids = _seed(store, 2)
    store.set_capability_fields(ids[0], confidence=1.0, needs_review=False, novelty="pattern")
    prompts = []
    _run(monkeypatch, {str(ids[1]): {"novelty": "commodity", "provides": [], "requires": []}},
         prompts)
    r = enrich.enrich(store, all_=True)
    assert r["skipped_reviewed"] == 1 and r["updated"] == 1
    assert f"{ids[0]} | o/r" not in prompts[0] and f"{ids[1]} | o/r" in prompts[0]
    assert store.get_capability(ids[0]).confidence == 1.0


def test_force_includes_reviewed(store, monkeypatch):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], confidence=1.0, novelty="pattern")
    prompts = []
    _run(monkeypatch, {str(ids[0]): {"novelty": "commodity", "provides": [], "requires": []}},
         prompts)
    r = enrich.enrich(store, all_=True, force=True)
    assert r["skipped_reviewed"] == 0 and f"{ids[0]} | o/r" in prompts[0]
    c = store.get_capability(ids[0])
    assert c.novelty == "commodity" and c.confidence is None


def test_acceptance_locks(store, monkeypatch):
    ids = _seed(store, 3)
    store.set_capability_fields(ids[0], acceptance=["input 3 gives 7"],
                                deepened={"at": "t", "sha": "s", "model": "m"})
    store.set_capability_fields(ids[1], acceptance=["existing check"])
    prompts = []
    new = {"acceptance": ["fresh check"]}
    _run(monkeypatch, {str(i): new for i in ids}, prompts)
    r = enrich.enrich(store, all_=True, fields=("acceptance",))
    assert r["skipped_acceptance"] == 2 and r["updated"] == 1
    assert [store.get_capability(i).acceptance for i in ids] == [
        ["input 3 gives 7"], ["existing check"], ["fresh check"]]
    assert f"{ids[0]} | o/r" not in prompts[0] and f"{ids[1]} | o/r" not in prompts[0]
    # deepened with empty acceptance is still locked
    store.set_capability_fields(ids[2], acceptance=[], deepened={"at": "t", "sha": "s",
                                                                 "model": "m"})
    _run(monkeypatch, {str(ids[2]): new})
    assert enrich.enrich(store, all_=True, fields=("acceptance",))["skipped_acceptance"] == 3
    # --force overrides both
    _run(monkeypatch, {str(i): new for i in ids})
    r = enrich.enrich(store, all_=True, fields=("acceptance",), force=True)
    assert r["updated"] == 3 and store.get_capability(ids[0]).acceptance == ["fresh check"]


def test_all_skipped_makes_no_model_call(store, monkeypatch):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], acceptance=["x"])

    def boom(*a, **k):
        raise AssertionError("model called")
    monkeypatch.setattr(claude_runner, "_invoke", boom)
    r = enrich.enrich(store, all_=True, fields=("acceptance",))
    assert r["updated"] == 0 and r["batches"] == 0 and r["failed_batches"] == []
    assert "no model call" in r["text"] and r["skipped_acceptance"] == 1


def test_cli_force_flag(store):
    from reincarnate import cli
    ns = cli.build_parser().parse_args(["enrich", "--all", "--force"])
    assert ns.force is True


# -- jev --------------------------------------------------------------------------------------
def test_jev_low_confidence_not_applied_and_flagged(store):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], provides=["auth:zzz-login"], novelty="pattern")
    canon = next(t for t in store.get_vocabulary().tokens if t.startswith("auth:"))
    fc = FixedClient(level=2, nov_conf=0.4, map_to=canon, map_conf=0.3)
    r = enrich.enrich(store, all_=True, engine="jev", client=fc)
    c = store.get_capability(ids[0])
    assert c.provides == ["auth:zzz-login"] and c.novelty == "pattern"
    assert c.needs_review is True and c.confidence == 0.3
    assert r["held_for_review"] == 1 and r["mapped_tokens"] == 0


def test_jev_no_questions_leaves_confidence_and_flag(store):
    ids = _seed(store, 1)
    canon = next(t for t in store.get_vocabulary().tokens if t.startswith("auth:"))
    store.set_capability_fields(ids[0], provides=[canon], confidence=0.2, needs_review=True)
    fc = FixedClient()
    r = enrich.enrich(store, all_=True, engine="jev", client=fc, fields=("interfaces",))
    c = store.get_capability(ids[0])
    assert (c.confidence, c.needs_review) == (0.2, True) and c.provides == [canon]
    assert r["updated"] == 1 and r["held_for_review"] == 0


def test_jev_skips_reviewed(store):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], confidence=1.0, novelty="pattern")
    fc = FixedClient(level=0)
    r = enrich.enrich(store, all_=True, engine="jev", client=fc)
    assert r["skipped_reviewed"] == 1 and fc.stats["calls"] == 0
    assert store.get_capability(ids[0]).novelty == "pattern"


# -- only-missing and interfaces_rated_at -----------------------------------------------------
def test_only_missing_applies_only_missing_fields(store, monkeypatch):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], provides=["auth:session"], requires=["ext:docker"])
    _run(monkeypatch, {str(ids[0]): {"novelty": "pattern", "provides": ["data:other"],
                                     "requires": []}})
    r = enrich.enrich(store, all_=True, only_missing=True)
    c = store.get_capability(ids[0])
    assert r["updated"] == 1 and c.novelty == "pattern"
    assert (c.provides, c.requires) == (["auth:session"], ["ext:docker"])


def test_interfaces_rated_at_set_and_empty_not_reselected(store, monkeypatch):
    ids = _seed(store, 1)
    assert len(enrich.select(store, all_=True, only_missing=True)) == 1
    _run(monkeypatch, {str(ids[0]): {"novelty": "pattern", "provides": [], "requires": []}})
    enrich.enrich(store, all_=True, only_missing=True)
    c = store.get_capability(ids[0])
    assert c.interfaces_rated_at and c.provides == [] and c.requires == []
    assert enrich.select(store, all_=True, only_missing=True) == []


def test_ingest_with_provides_key_stamps(store):
    d = make_dossier(("a", "x"), ("b", "y"))
    d["capabilities"][0]["provides"] = []
    store.upsert_dossier("o", "r", "u", "s", d)
    a, b = store.list_capabilities()
    assert a.interfaces_rated_at and b.interfaces_rated_at == ""
    d["capabilities"][1]["requires"] = []  # a re-ingest that now carries the key
    store.upsert_dossier("o", "r", "u", "s", d)
    assert all(c.interfaces_rated_at for c in store.list_capabilities())


# -- import-interfaces ------------------------------------------------------------------------
def _imp_store(store):
    store.upsert_dossier("Acme", "Widget", "u", "s", make_dossier(
        ("Health Pinger", "pings"), ("icon library", "svgs"), ("untouched", "x")))
    return {c.name: c for c in store.list_capabilities()}


def _path():
    from pathlib import Path
    return Path(__file__).parent / "fixtures" / FIX


def test_import_keeps_existing_by_default_and_counts(store):
    caps = _imp_store(store)
    store.set_capability_fields(caps["Health Pinger"].id, provides=["auth:session"],
                                confidence=0.9)
    r = imp.import_interfaces(store, _path())
    assert (r["matched"], r["changed"], r["skipped_existing"], r["skipped_empty"]) == (2, 1, 1, 0)
    assert r["unmatched_count"] == 1 and "1 changed" in r["text"]
    now = {c.name: c for c in store.list_capabilities()}
    assert now["Health Pinger"].provides == ["auth:session"]
    assert now["Health Pinger"].confidence == 0.9
    assert now["icon library"].provides == ["ui:icon-set"]
    assert now["icon library"].confidence is None


def test_import_overwrite_changes_and_nulls_confidence(store):
    caps = _imp_store(store)
    store.set_capability_fields(caps["Health Pinger"].id, provides=["auth:session"],
                                confidence=0.9, needs_review=True)
    r = imp.import_interfaces(store, _path(), overwrite=True)
    assert r["changed"] == 2 and r["skipped_existing"] == 0
    c = store.get_capability(caps["Health Pinger"].id)
    assert c.provides == ["data:service-status"] and c.confidence is None
    assert c.needs_review is True  # flag left as is
    again = imp.import_interfaces(store, _path(), overwrite=True)
    assert again["changed"] == 0 and again["unchanged"] == 2


def test_import_skips_empty_entries(store, tmp_path):
    caps = _imp_store(store)
    store.set_capability_fields(caps["icon library"].id, provides=["ui:icon-set"])
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"items": {
        "40-Code/repos/Acme-Widget.md::icon library": {"provides": [], "requires": []},
        "40-Code/repos/Acme-Widget.md::untouched": {}}}))
    r = imp.import_interfaces(store, f, overwrite=True)
    assert r["skipped_empty"] == 2 and r["changed"] == 0
    assert store.get_capability(caps["icon library"].id).provides == ["ui:icon-set"]


def test_jev_never_writes_tier3_confidence(store):
    ids = _seed(store, 1)
    fc = FixedClient(level=2, nov_conf=1.0)
    enrich.enrich(store, all_=True, engine="jev", client=fc, fields=("novelty",))
    assert store.get_capability(ids[0]).confidence == enrich.JEV_MAX_CONFIDENCE < 1.0
    r = enrich.enrich(store, all_=True, engine="jev", client=fc, fields=("novelty",))
    assert r["skipped_reviewed"] == 0 and r["updated"] == 1  # Tier 2 never locks itself

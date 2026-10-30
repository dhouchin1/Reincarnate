"""The studio's fit check."""

import pytest

from p2_catalog import _cap, _dossier, seed
from reincarnate import fitcheck
from reincarnate.models import Project


@pytest.fixture
def ids(store):
    return seed(store)


def test_wired_unmet_settings_and_kit_steps(store, ids):
    out = fitcheck.fit(store, [ids["Team dashboard"], ids["Layout grid"]])
    assert out["wired"] == [{"consumer": "Team dashboard", "token": "store:board-layout",
                             "provider": "Layout grid"}]
    assert [u["token"] for u in out["unmet"]] == ["auth:session"]
    cand = out["unmet"][0]["candidates"]
    assert cand == [{"id": ids["Session login"], "name": "Session login",
                     "donor": "acme/authkit", "license": "MIT"}]
    assert out["settings"] == ["config:theme (for Team dashboard)"]
    assert out["external"] == []
    assert out["kit_steps"] == {"total": 5, "scaffold": 1, "parts": 2, "fresh": 1, "glue": 1}


def test_external_needs_and_no_wiring(store, ids):
    out = fitcheck.fit(store, [ids["Email notifier"]])
    assert out["external"] == ["ext:smtp (for Email notifier)"]
    assert [u["token"] for u in out["unmet"]] == ["event:activity"]
    solo = fitcheck.fit(store, [ids["Layout grid"]])
    assert solo["wired"] == [] and solo["unmet"] == []
    assert solo["kit_steps"] == {"total": 2, "scaffold": 1, "parts": 1, "fresh": 0, "glue": 0}


def test_candidates_skip_denied_licenses_and_put_the_same_donor_first(store, ids):
    store.upsert_dossier("acme", "ops", "https://github.com/acme/ops", "o1", _dossier("MIT", [
        _cap("Reporter", "weekly report of team activity", requires=["schedule:daily"]),
        _cap("Cron lite", "run jobs on a daily schedule", provides=["schedule:daily"])]))
    store.upsert_dossier("zed", "cron", "https://github.com/zed/cron", "z1", _dossier("MIT", [
        _cap("Zed cron", "daily schedule runner for jobs", provides=["schedule:daily"])]))
    names = {c.name: c.id for c in store.list_capabilities()}
    out = fitcheck.fit(store, [names["Reporter"]])
    cands = out["unmet"][0]["candidates"]
    assert [c["name"] for c in cands] == ["Cron lite", "Zed cron"]  # same donor first
    assert "Digest scheduler" not in [c["name"] for c in cands]  # AGPL is denied
    assert all(set(c) == {"id", "name", "donor", "license"} for c in cands)


def test_licenses_group_by_license_with_level_and_review(store, ids):
    out = fitcheck.fit(store, [ids["Session login"], ids["Layout grid"], ids["Email notifier"]])
    by = {x["license"]: x for x in out["licenses"]}
    assert by["MIT"] == {"license": "MIT", "level": "ok", "review": False,
                         "parts": ["Session login", "Layout grid"]}
    assert by["AGPL-3.0"]["level"] == "deny" and by["AGPL-3.0"]["review"] is True
    assert by["AGPL-3.0"]["parts"] == ["Email notifier"]


def test_projects_take_the_best_score_per_project(store, ids):
    store.add_project(Project(slug="other", path="/x/other"))
    store.set_fit(ids["Layout grid"], "homelab", 0.4, "ok", "overlay")
    store.set_fit(ids["Session login"], "homelab", 0.9, "great", "overlay")
    store.set_fit(ids["Session login"], "other", 0.2, "meh", "overlay")
    store.set_fit(ids["Activity feed"], "other", 0.99, "unrelated", "overlay")
    out = fitcheck.fit(store, [ids["Layout grid"], ids["Session login"]])
    assert out["projects"] == [{"slug": "homelab", "score": 0.9}, {"slug": "other", "score": 0.2}]


def test_deepened_count_and_cost(store, ids):
    store.conn.execute("UPDATE capabilities SET deepened_at='2026-01-01' WHERE id=?",
                       (ids["Layout grid"],))
    out = fitcheck.fit(store, [ids["Layout grid"], ids["Session login"], ids["Activity feed"]])
    assert out["deepened"] == {"n": 1, "of": 3}
    assert out["deepen_cost"] == pytest.approx(0.022)


def test_bad_ids_and_empty_portfolio(store, ids):
    with pytest.raises(ValueError, match="no such part"):
        fitcheck.fit(store, [99999])
    store.conn.execute("UPDATE capabilities SET retired_at='2026-01-01' WHERE id=?",
                       (ids["Layout grid"],))
    with pytest.raises(ValueError, match="retired"):
        fitcheck.fit(store, [ids["Layout grid"]])
    out = fitcheck.fit(store, [])
    assert out["kit_steps"]["total"] == 1 and out["deepened"] == {"n": 0, "of": 0}


def test_duplicate_ids_count_once(store, ids):
    out = fitcheck.fit(store, [ids["Layout grid"], ids["Layout grid"]], roles={"1": "x"})
    assert out["kit_steps"]["parts"] == 1


def test_every_registered_project_is_listed_even_with_no_fit(store, ids):
    store.add_project(Project(slug="quiet", path="/x/quiet", goal="nothing here"))
    out = fitcheck.fit(store, [ids["Layout grid"]])
    assert {p["slug"] for p in out["projects"]} >= {"homelab", "quiet"}
    assert next(p for p in out["projects"] if p["slug"] == "quiet")["score"] == 0.0

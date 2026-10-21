from types import SimpleNamespace

import pytest

from reincarnate import catalog
from reincarnate.models import Profile, Project
from reincarnate.store import Store

DATA = {
    "summary": "donor",
    "dependencies": {"license": "MIT"},
    "capabilities": [
        {"name": "health-check pinger", "what": "pings services and reports health status",
         "portability": "high"},
        {"name": "icon library", "what": "svg glyph sprites", "portability": "high"},
        {"name": "media-calendar", "what": "calendar of upcoming jellyfin releases",
         "portability": "medium"},
    ],
}


@pytest.fixture
def env(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    s.save_profile(Profile(name="d", intents=["health pinging"]))
    s.add_project(Project(slug="homelab-console", stack=["python", "fastapi"],
                          goal="dashboard for self-hosted services with health checks"))
    s.add_project(Project(slug="media-hub", stack=["typescript"],
                          goal="jellyfin and sonarr media requests"))
    s.upsert_dossier("o", "r", "u", "abc", DATA)
    s.upsert_dossier("p", "q", "u", "def", {"capabilities": [
        {"name": "other thing", "what": "unrelated", "portability": "low"}]})
    yield s
    s.close()


def cid(s, name):
    return next(c.id for c in s.list_capabilities() if c.name == name)


def names(entries):
    return [e.name for e in entries]


def test_search_and_filters(env):
    s = env
    assert names(catalog.build_catalog(s, search="health"))[0] == "health-check pinger"
    assert set(names(catalog.build_catalog(s, portability="high"))) == {
        "health-check pinger", "icon library"}
    assert set(names(catalog.build_catalog(s, donor="p/q"))) == {"other thing"}
    s.set_fit(cid(s, "media-calendar"), "media-hub", 0.7, "x", "overlay")
    s.set_fit(cid(s, "icon library"), "media-hub", 0.1, "x", "overlay")
    assert names(catalog.build_catalog(s, fit="media-hub")) == ["media-calendar"]


def test_votes_reorder(env):
    s = env
    assert names(catalog.build_catalog(s, portability="high")) == [
        "health-check pinger", "icon library"]
    s.set_vote(cid(s, "icon library"), 1)
    out = catalog.build_catalog(s, portability="high")
    assert names(out) == ["icon library", "health-check pinger"]
    assert out[0].vote == 1 and out[0].score == 1.25


def test_score_uses_best_fit(env):
    s = env
    s.set_fit(cid(s, "media-calendar"), "media-hub", 0.8, "x", "overlay")
    e = next(e for e in catalog.build_catalog(s) if e.name == "media-calendar")
    assert e.score == pytest.approx(0.6 + 0.4)
    assert e.fit[0]["project"] == "media-hub"


def test_summary(env):
    s = env
    s.set_vote(cid(s, "icon library"), 1)
    s.set_fit(cid(s, "media-calendar"), "media-hub", 0.8, "x", "overlay")
    sm = catalog.summary(catalog.build_catalog(s))
    assert sm == {"total": 4, "by_portability": {"high": 2, "medium": 1, "low": 1},
                  "donors": 2, "with_fit": 1, "voted": 1}


def test_run_cli_catalog(env):
    s = env
    ns = SimpleNamespace(search=None, portability=None, donor=None, fit=None, gaps=False)
    r = catalog.run_cli(s, ns)
    assert r["summary"]["total"] == 4 and len(r["items"]) == 4
    assert "health-check pinger" in r["text"] and "o/r" in r["text"]


def test_text_truncates(env):
    es = [catalog.CatalogEntry(capability_id=i, name=f"n{i}") for i in range(65)]
    assert catalog._table(es).splitlines()[-1] == "… 5 more"


def test_gaps(env):
    s = env
    s.set_fit(cid(s, "health-check pinger"), "homelab-console", 0.8, "x", "overlay")
    r = catalog.run_cli(s, SimpleNamespace(gaps=True))
    g = {x["project"]: x for x in r["gaps"]}
    assert g["homelab-console"]["covered"] == ["health-check pinger"]
    assert g["media-hub"]["covered"] == []
    assert "sonarr" in g["media-hub"]["uncovered_terms"]
    assert "jellyfin" not in g["media-hub"]["uncovered_terms"]
    intent = g[None]
    assert intent["intent"] == "health pinging"
    assert "health-check pinger" in intent["matches"]
    assert isinstance(r["text"], str)

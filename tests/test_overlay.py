import json
from types import SimpleNamespace

import pytest

from reincarnate import claude_runner, overlay
from reincarnate.models import Profile, Project
from reincarnate.store import Store

DATA = {
    "summary": "donor",
    "dependencies": {"license": "MIT"},
    "capabilities": [
        {"name": "health-check pinger", "what": "pings services and reports health status",
         "portability": "high", "dependencies": ["httpx"]},
        {"name": "icon library", "what": "svg glyph sprites", "portability": "high"},
        {"name": "media-calendar", "what": "calendar of upcoming jellyfin and sonarr releases",
         "portability": "medium"},
    ],
}


@pytest.fixture
def env(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    profile = s.save_profile(Profile(name="d", stack=["python"], intents=["monitor services"]))
    s.add_project(Project(slug="homelab-console", stack=["python", "fastapi"],
                          goal="dashboard for self-hosted services with health checks"))
    s.add_project(Project(slug="media-hub", stack=["typescript"],
                          goal="jellyfin and sonarr media requests"))
    d = s.upsert_dossier("o", "r", "https://github.com/o/r", "abc", DATA)
    yield s, d, profile, s.list_projects()
    s.close()


def cap_id(s, name):
    return next(c.id for c in s.list_capabilities() if c.name == name)


def test_tokens():
    assert overlay._tokens("The Health-check of AI") == {"health", "check"}


def test_heuristic_fits(env):
    s, d, profile, projects = env
    out = {c.name: overlay.heuristic_fits(c, projects) for c in s.list_capabilities()}
    assert out["health-check pinger"][0][0] == "homelab-console"
    assert out["health-check pinger"][0][1] >= 0.15
    assert out["health-check pinger"][0][2].startswith("shares terms:")
    assert out["media-calendar"][0][0] == "media-hub"


def test_apply_heuristic_respects_manual(env):
    s, d, profile, projects = env
    pid = cap_id(s, "health-check pinger")
    s.set_fit(pid, "homelab-console", 0.99, "mine", "manual")
    r = overlay.apply(s, d, profile, projects, engine="heuristic")
    assert r["engine_used"] == "heuristic" and r["capabilities"] == 3 and r["fits_written"] >= 1
    fits = {(f.capability_id, f.project_slug): f for f in s.list_fits()}
    assert fits[(pid, "homelab-console")].source == "manual"
    assert fits[(pid, "homelab-console")].score == 0.99
    cal = fits[(cap_id(s, "media-calendar"), "media-hub")]
    assert cal.source == "overlay" and cal.score >= 0.15


def _stub(monkeypatch, text, rc=0):
    out = json.dumps({"type": "result", "result": text}).encode()
    monkeypatch.setattr(claude_runner, "_invoke", lambda *a, **k: (rc, out, b""))


def test_claude_p_engine(env, monkeypatch):
    s, d, profile, projects = env
    arr = [{"capability": "Health-Check Pinger", "project": "homelab-console", "score": 0.9,
            "how": "drop in"},
           {"capability": "nope", "project": "homelab-console", "score": 0.9, "how": ""},
           {"capability": "icon library", "project": "ghost", "score": 0.9, "how": ""}]
    _stub(monkeypatch, "here:\n```json\n" + json.dumps(arr) + "\n```")
    r = overlay.apply(s, d, profile, projects, engine="claude-p")
    assert r["engine_used"] == "claude-p" and r["fits_written"] == 1
    (f,) = s.list_fits()
    assert (f.project_slug, f.score, f.source, f.how) == ("homelab-console", 0.9, "overlay",
                                                          "drop in")


def test_claude_p_garbage_falls_back(env, monkeypatch):
    s, d, profile, projects = env
    _stub(monkeypatch, "sorry, no idea")
    r = overlay.apply(s, d, profile, projects, engine="claude-p")
    assert r["engine_used"] == "heuristic" and r["fits_written"] >= 1
    assert "heuristic" in r["message"]


def test_claude_p_missing_prompt_builder(env, monkeypatch):
    from reincarnate import prompts
    s, d, profile, projects = env
    monkeypatch.delattr(prompts, "build_overlay_prompt")
    r = overlay.apply(s, d, profile, projects, engine="claude-p")
    assert r["engine_used"] == "heuristic" and "missing" in r["message"]


def test_extract_json_array():
    assert overlay._extract_json_array('x [1, 2] y') == [1, 2]
    assert overlay._extract_json_array('{"a": 1}') is None
    assert overlay._extract_json_array("") is None


def test_run_cli(env):
    s, d, profile, projects = env
    ns = SimpleNamespace
    assert "error" in overlay.run_cli(s, ns(repo=None, all=False, engine="heuristic"))
    assert "error" in overlay.run_cli(s, ns(repo="x/y", all=False, engine="heuristic"))
    r = overlay.run_cli(s, ns(repo="o/r", all=False, engine="heuristic"))
    assert r["total_fits"] == r["results"][0]["fits_written"] >= 2
    r = overlay.run_cli(s, ns(repo=None, all=True, engine="heuristic"))
    assert len(r["results"]) == 1

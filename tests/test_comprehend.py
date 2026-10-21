import json
import subprocess

import pytest

from reincarnate import claude_runner, comprehend
from reincarnate.models import Dossier, Profile, Project

DOSSIER = {
    "summary": "s",
    "what_it_does": "w",
    "capabilities": [
        {"name": "A", "what": "a", "portability": "high"},
        {"name": "B", "what": "b", "portability": "low"},
    ],
}


@pytest.mark.parametrize("url", [
    "https://github.com/o/r", "https://github.com/o/r.git", "https://github.com/o/r/",
    "git@github.com:o/r.git", "o/r",
])
def test_parse_ok(url):
    assert comprehend.parse_github_url(url) == ("o", "r")


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b", "not a url", "", "a/b/c"])
def test_parse_rejects(url):
    with pytest.raises(ValueError):
        comprehend.parse_github_url(url)


def _git_repo(tmp_path):
    src = tmp_path / "src" / "myrepo"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("hi")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(tmp_path)}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=src, env=env, check=True)
    return src


def test_clone_local_and_reuse(tmp_path):
    src = _git_repo(tmp_path)
    dst = tmp_path / "dst"
    first = comprehend.clone_repo(str(src), dest=dst, allow_local=True)
    assert len(first["sha"]) == 40 and set(first["sha"]) <= set("0123456789abcdef")
    assert first["reused"] is False
    assert first["owner"] == "src" and first["repo"] == "myrepo"
    hp = subprocess.run(["git", "config", "core.hooksPath"], cwd=dst, capture_output=True,
                        text=True).stdout.strip()
    assert hp == "/dev/null"
    second = comprehend.clone_repo(str(src), dest=dst, allow_local=True)
    assert second["reused"] is True and second["sha"] == first["sha"]


def test_clone_refuses_local_by_default(tmp_path):
    with pytest.raises(ValueError):
        comprehend.clone_repo(str(tmp_path), dest=tmp_path / "d")


def test_clone_size_cap(tmp_path, monkeypatch):
    src = _git_repo(tmp_path)
    dst = tmp_path / "dst"
    monkeypatch.setattr(comprehend, "_du_kb", lambda p: 600000)
    with pytest.raises(RuntimeError):
        comprehend.clone_repo(str(src), dest=dst, allow_local=True)
    assert not dst.exists()


class FakeStore:
    def __init__(self, existing=None):
        self.upserts = []
        self.existing = existing

    def get_dossier(self, ref):
        return self.existing

    def get_profile(self):
        return Profile(stack=["python"])

    def list_projects(self):
        return [Project(slug="alpha", stack=["go"])]

    def upsert_dossier(self, owner, repo, url, sha, data, *, model="", cost_usd=0.0,
                       source="claude-p"):
        self.upserts.append({"owner": owner, "repo": repo, "sha": sha, "data": data,
                             "source": source, "cost": cost_usd, "model": model})
        return Dossier(id=5, owner=owner, repo=repo, url=url, sha=sha, model=model,
                       source=source, data=data)


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    monkeypatch.setenv("REINCARNATE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(comprehend, "clone_repo", lambda url, **kw: {
        "path": str(tmp_path), "owner": "o", "repo": "r", "sha": "b" * 40, "reused": False})
    return tmp_path


def _stub_claude(monkeypatch, result_text, cost=0.12):
    out = json.dumps({"type": "result", "result": result_text, "total_cost_usd": cost})
    calls = []

    def fake(argv, cwd, env, timeout):
        calls.append(argv)
        return 0, out.encode(), b""

    monkeypatch.setattr(claude_runner, "_invoke", fake)
    return calls


def test_comprehend_url_ok(sandboxed, monkeypatch):
    text = "I looked around.\n\n```json\n" + json.dumps(DOSSIER) + "\n```\n"
    calls = _stub_claude(monkeypatch, text)
    store = FakeStore()
    res = comprehend.comprehend_url(store, "o/r")
    assert len(calls) == 1
    assert len(store.upserts) == 1 and store.upserts[0]["source"] == "claude-p"
    assert res["capabilities"] == 2 and res["parsed"] is True
    assert res["donor"] == "o/r" and res["cost_usd"] == 0.12
    note = open(res["note_path"]).read()
    assert "## Capabilities (2)" in note


def test_comprehend_url_no_json(sandboxed, monkeypatch):
    _stub_claude(monkeypatch, "sorry, no dossier today")
    store = FakeStore()
    res = comprehend.comprehend_url(store, "o/r")
    assert res["parsed"] is False and res["message"]
    assert store.upserts == []
    from reincarnate import paths
    raw = paths.dossiers_dir() / "o-r.raw.txt"
    assert raw.read_text() == "sorry, no dossier today"


def test_comprehend_url_skips_existing(sandboxed, monkeypatch):
    calls = _stub_claude(monkeypatch, "x")
    store = FakeStore(existing=Dossier(id=9, owner="o", repo="r"))
    res = comprehend.comprehend_url(store, "o/r")
    assert res["skipped"] is True and calls == []
    text = "```json\n" + json.dumps(DOSSIER) + "\n```"
    _stub_claude(monkeypatch, text)
    assert comprehend.comprehend_url(store, "o/r", force=True)["parsed"] is True

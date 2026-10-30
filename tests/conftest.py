import pytest

from reincarnate.store import Store


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("REINCARNATE_HOME", str(tmp_path / "home"))
    # no test may reach the real `claude` (it costs money and needs a login); a test that wants a
    # fake binary sets REINCARNATE_CLAUDE_BIN itself
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", str(tmp_path / "no-real-claude"))
    # the idea planner is the default; the older tests exercise the parts planner, and the
    # idea-planner tests pass planner="idea" explicitly
    monkeypatch.setenv("REINCARNATE_PLANNER", "parts")
    return tmp_path / "home"


@pytest.fixture
def store():
    s = Store()
    yield s
    s.close()


def make_dossier(*caps, fit=None):
    return {
        "summary": "A tool.",
        "dependencies": {"license": "MIT"},
        "capabilities": [
            {"name": n, "what": w, "portability": "high", "files": ["a.py"]} for n, w in caps
        ],
        "fit_to_projects": fit or [],
    }

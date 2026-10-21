import pytest

from reincarnate.store import Store


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("REINCARNATE_HOME", str(tmp_path / "home"))
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

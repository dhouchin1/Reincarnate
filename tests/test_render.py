import json
from types import SimpleNamespace

from reincarnate import paths, render
from reincarnate.models import Dossier

DATA = {
    "summary": "a thing",
    "what_it_does": "does things",
    "languages": ["python"],
    "build_system": "hatch",
    "entry_points": ["main.py - cli"],
    "architecture": "two parts",
    "capabilities": [
        {"name": "Queue", "what": "queues | jobs", "files": ["q.py"], "dependencies": ["redis"],
         "portability": "high", "portability_notes": "easy", "security_notes": "eval used"},
    ],
    "dependencies": {"license": "MIT", "notable": ["redis"], "supply_chain_flags": []},
    "security": {"concerns": ["eval"], "network_egress": "none", "dangerous_patterns": []},
    "fit_to_projects": [{"project": "alpha", "capability": "Queue", "how": "swap in"}],
    "salvage_recommendations": ["rebuild the queue"],
}


def _dossier():
    return Dossier(id=1, owner="o", repo="r", url="https://github.com/o/r", sha="a" * 40,
                   comprehended_at="2026-01-01", model="sonnet", source="claude-p",
                   license="MIT", data=DATA)


def test_render_sections_and_roundtrip():
    md = render.render_dossier_markdown(_dossier())
    for h in ("# o/r", "## What it does", "## Architecture", "## Capabilities (1)",
              "## Dependencies & license", "## Security review", "## Fit to your projects",
              "## Salvage recommendations", "## Raw analysis (machine-readable)"):
        assert h in md
    order = [md.index(h) for h in ("## What it does", "## Architecture", "## Capabilities",
                                   "## Dependencies", "## Security", "## Fit",
                                   "## Salvage", "## Raw")]
    assert order == sorted(order)
    assert md.startswith("---\ntype: repo-dossier\n")
    assert "| alpha | Queue | swap in |" in md
    assert "**Security notes:** eval used" in md
    block = md.split("```json\n")[1].rsplit("```", 1)[0]
    assert json.loads(block) == DATA


def test_render_empty_data():
    d = Dossier(owner="o", repo="r", data={})
    md = render.render_dossier_markdown(d)
    assert "## Capabilities (0)" in md and "## Raw analysis" in md


class FakeStore:
    def get_dossier(self, ref):
        return _dossier() if ref == "o/r" else None


def test_run_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("REINCARNATE_HOME", str(tmp_path))
    res = render.run_cli(FakeStore(), SimpleNamespace(ref="o/r", out=None))
    assert res["donor"] == "o/r"
    assert res["path"] == str(paths.dossiers_dir() / "o-r.md")
    out = tmp_path / "x" / "y.md"
    res = render.run_cli(FakeStore(), SimpleNamespace(ref="o/r", out=str(out)))
    assert out.exists() and res["path"] == str(out)
    assert "error" in render.run_cli(FakeStore(), SimpleNamespace(ref="no/pe", out=None))

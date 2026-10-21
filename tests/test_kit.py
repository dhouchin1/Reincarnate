import os
import zipfile
from types import SimpleNamespace

import pytest

from reincarnate import kit, paths
from reincarnate.models import Composition, Profile, Project

CONCERN = "Parser executes shell commands from config"
INTENT = "Wire the parser into the CLI loader."


def seed(store, tmp_path):
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.add_project(Project(slug="proj", path=str(tmp_path / "proj"), stack=["python"],
                              test_cmd="pytest -q"))
    data = {
        "summary": "A tool.",
        "dependencies": {"license": "MIT"},
        "security": {"concerns": [CONCERN, "unrelated issue"]},
        "fit_to_projects": [{"project": "proj", "capability": "Parser", "how": INTENT}],
        "capabilities": [
            {"name": "Parser", "what": "Parses config files into typed records.",
             "files": ["src/parser.py", "src/util.py"], "portability": "high",
             "portability_notes": "Keep it pure.", "security_notes": "Validate input."},
            {"name": "Cache", "what": "Caches results.", "files": ["cache.py"],
             "portability": "medium"},
        ],
    }
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", data)
    return store.list_capabilities()


def make_comp(store, tmp_path):
    caps = seed(store, tmp_path)
    c = Composition(
        slug="my-app", name="My App", pitch="Does things", idea="an app", target_kind="project",
        target_slug="proj",
        proposal={
            "capabilities": [{"capability_id": caps[0].id, "role": "parsing", "order": 1},
                             {"capability_id": caps[1].id, "role": "caching", "order": 2}],
            "glue": "Glue the two together.",
            "gaps": [{"need": "Auth layer", "suggested_donors": ["x/y"], "build_fresh": True}],
            "risks": ["Parser is slow"],
            "license_flags": [{"donor": "own/repo", "license": "MIT", "level": "warn"}],
        })
    return store.save_composition(c)


def test_export_to_project(store, tmp_path):
    c = make_comp(store, tmp_path)
    (tmp_path / "proj").mkdir()
    r = kit.export_kit(store, c, "proj")
    kd = tmp_path / "proj" / ".reincarnate"
    assert r["kit_dir"] == str(kd) and r["steps"] == 3 and r["zip"] is None
    for f in ["BLUEPRINT.md", "CLAUDE.md", "tasks.md", "run.sh"]:
        assert (kd / f).is_file()
    tasks = (kd / "tasks.md").read_text()
    assert tasks.count("- [ ]") == 3 and tasks.count("  spec: .reincarnate/specs/") == 3
    names = sorted(p.name for p in (kd / "specs").iterdir())
    assert [n[:3] for n in names] == ["01-", "02-", "03-"]
    run = (kd / "run.sh").read_text()
    assert os.access(kd / "run.sh", os.X_OK)
    assert "claude -p" in run and "--permission-mode acceptEdits" in run
    assert len(run.splitlines()) < 60
    bp = (kd / "BLUEPRINT.md").read_text()
    assert "own/repo | MIT" in bp and "[warn]" in bp and "Glue the two together." in bp
    assert "Step 3 — build fresh: Auth layer" in bp and "Parser is slow" in bp
    assert "pytest -q" in (kd / "CLAUDE.md").read_text()
    assert "pytest -q" in run
    assert store.get_composition(c.id).status == "emitted"
    assert store.conn.execute("SELECT COUNT(*) FROM kits").fetchone()[0] == 1


def test_zip_and_notes(store, tmp_path):
    c = make_comp(store, tmp_path)
    r = kit.export_kit(store, c, "proj", zip_it=True, notes="custom glue")
    assert r["zip"].startswith(str(paths.kits_dir()))
    with zipfile.ZipFile(r["zip"]) as z:
        assert any(n.endswith("BLUEPRINT.md") for n in z.namelist())
    assert "custom glue" in (tmp_path / "proj" / ".reincarnate" / "BLUEPRINT.md").read_text()


def test_new_target(store, tmp_path):
    c = make_comp(store, tmp_path)
    r = kit.export_kit(store, c, "new:Fresh App")
    assert r["kit_dir"] == str(paths.data_dir() / "new" / "fresh-app" / ".reincarnate")
    assert (paths.data_dir() / "new" / "fresh-app" / ".reincarnate" / "tasks.md").is_file()


def test_directory_target_and_empty(store, tmp_path):
    c = make_comp(store, tmp_path)
    d = tmp_path / "somedir"
    d.mkdir()
    assert kit.export_kit(store, c, str(d))["steps"] == 3
    empty = Composition(slug="e", name="E", proposal={"capabilities": [], "gaps": []})
    store.save_composition(empty)
    with pytest.raises(ValueError):
        kit.export_kit(store, empty, "proj")
    with pytest.raises(ValueError):
        kit.export_kit(store, c, str(tmp_path / "nope"))


def test_run_cli(store, tmp_path):
    c = make_comp(store, tmp_path)
    notes = tmp_path / "n.md"
    notes.write_text("from file")
    r = kit.run_cli(store, SimpleNamespace(composition="my-app", into="proj", zip=False,
                                           notes=str(notes)))
    assert r["steps"] == 3 and "error" not in r
    assert "error" in kit.run_cli(store, SimpleNamespace(composition="zzz", into="proj",
                                                         zip=False))
    assert c.id

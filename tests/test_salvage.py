from types import SimpleNamespace

from reincarnate import salvage
from reincarnate.models import Profile, Project

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


def test_spec_sections(store, tmp_path):
    caps = seed(store, tmp_path)
    spec = salvage.build_clean_room_spec(store, caps[0], target_slug="proj")
    for s in ["## Clean-room reimplementation: Parser", "**Goal:**", "**Provenance:**",
              "own/repo", "MIT", "comprehended", "Constraints — the clean-room air gap",
              "**Behavior to implement:**", "Parses config files", "Design notes", "Keep it pure",
              "**Security requirements:**", CONCERN, "Validate input.", "Integration intent",
              INTENT, "src/parser.py", "src/util.py", "DO NOT COPY", "`pytest -q`", "python"]:
        assert s in spec, s
    assert "unrelated issue" not in spec
    assert "def " not in spec and "```" not in spec


def test_spec_new_target(store, tmp_path):
    caps = seed(store, tmp_path)
    spec = salvage.build_clean_room_spec(store, caps[0], target_slug="new:thing",
                                         include_header=False)
    assert "the new project's stack" in spec
    assert "the suite runs with" not in spec
    assert "Clean-room reimplementation" not in spec
    assert INTENT not in spec


def test_fresh_spec():
    spec = salvage.build_fresh_spec(
        {"need": "Billing webhooks", "suggested_donors": ["a/b"], "build_fresh": True},
        target_slug="new:x")
    assert "build fresh" in spec and "Billing webhooks" in spec and "a/b" in spec


def test_run_cli(store, tmp_path):
    caps = seed(store, tmp_path)
    out = tmp_path / "o" / "spec.md"
    r = salvage.run_cli(store, SimpleNamespace(capability_id=caps[0].id, into="proj",
                                               out=str(out)))
    assert r["name"] == "Parser" and r["donor"] == "own/repo" and r["path"] == str(out)
    assert out.read_text() == r["spec"]
    r2 = salvage.run_cli(store, SimpleNamespace(capability_id=caps[0].id, into="proj"))
    assert r2["path"] is None
    assert "error" in salvage.run_cli(store, SimpleNamespace(capability_id=999, into="proj"))

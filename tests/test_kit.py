import os
import zipfile
from types import SimpleNamespace

import pytest

from reincarnate import kit, paths
from reincarnate.models import Composition, Profile, Project

CONCERN = "Parser executes shell commands from config"
INTENT = "Wire the parser into the CLI loader."


def seed(store, tmp_path):
    (tmp_path / "proj").mkdir(exist_ok=True)
    (tmp_path / "proj" / "pyproject.toml").write_text("[project]\nname = 'proj'\n")
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
    assert "TEST_CMD='pytest -q'" in run
    assert 'N=$((10#${f%%-*}))' in run
    # existing project with a manifest: no scaffold, its own stack and dependency policy
    assert not (kd / "specs" / "00-scaffold.md").exists() and "Step 0" not in tasks
    claude = (kd / "CLAUDE.md").read_text()
    assert "## Stack\n\n- python\n" in claude
    assert "Allowed dependencies: the dependencies this project already uses." in claude
    assert "set in step 0" not in claude
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
    assert kit.export_kit(store, c, str(d))["steps"] == 4  # no manifest: new, step 0 added
    assert (d / ".reincarnate" / "specs" / "00-scaffold.md").is_file()
    (d / "package.json").write_text("{}")
    assert kit.export_kit(store, c, str(d))["steps"] == 3
    assert "TEST_CMD='npm test'" in (d / ".reincarnate" / "run.sh").read_text()
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


def _kit(name="fresh"):
    return paths.data_dir() / "new" / name / ".reincarnate"


def _new_comp(store, tmp_path, stack, **extra):
    caps = seed(store, tmp_path)
    store.save_profile(Profile(name="dan", stack=["python", "fastapi"]))
    proposal = {"capabilities": [{"capability_id": caps[0].id, "role": "parsing", "order": 1},
                                 {"capability_id": caps[1].id, "role": "caching", "order": 2}],
                "stack": stack, "gaps": [{"need": "Auth layer", "build_fresh": True}]}
    proposal.update(extra)
    c = Composition(slug="fresh-app", name="Fresh", target_kind="new", target_slug="fresh",
                    proposal=proposal)
    return store.save_composition(c), caps


def test_new_target_uses_option_stack(store, tmp_path):
    c, _ = _new_comp(store, tmp_path, "TypeScript + Next.js + SQLite")
    r = kit.export_kit(store, c, "new:fresh")
    kd = _kit()
    assert r["steps"] == 4
    claude = (kd / "CLAUDE.md").read_text()
    stack = claude.split("## Stack\n\n")[1].split("\n\n")[0]
    assert stack == "- TypeScript\n- Next.js\n- SQLite"
    assert "fastapi" not in claude
    assert "## Dependencies" in claude and "## Tests" in claude and "## Clean-room rules" in claude
    assert "Test command: `npm test` (set in step 0)." in claude
    assert "Allowed dependencies: the stack below." in claude
    assert "TEST_CMD='npm test'" in (kd / "run.sh").read_text()
    assert sorted(p.name for p in (kd / "specs").iterdir()) == [
        "00-scaffold.md", "01-parser.md", "02-cache.md", "03-fresh-auth-layer.md"]
    tasks = (kd / "tasks.md").read_text().splitlines()
    assert tasks[:2] == ["- [ ] Step 0: scaffold the project",
                         "  spec: .reincarnate/specs/00-scaffold.md"]
    bp = (kd / "BLUEPRINT.md").read_text()
    steps = bp.split("## Steps\n\n")[1].split("\n\n")[0].splitlines()
    assert steps[0].startswith("0. Step 0 — scaffold the project (TypeScript, Next.js, SQLite")
    assert steps[1].startswith("1. Step 1 — Parser") and len(steps) == 4
    scaffold = (kd / "specs" / "00-scaffold.md").read_text()
    for s in ["`package.json` (node)", "`npm test` exits 0 with at least one test per ecosystem",
              "`DEPENDENCIES.md` covers every declared dependency",
              "No feature code from later steps", "\"in the stack\"", "Run the tests",
              "entry points only"]:
        assert s in scaffold, s


def test_new_target_mixed_stack_uses_make(store, tmp_path):
    c, _ = _new_comp(store, tmp_path, "Python, React")
    kit.export_kit(store, c, "new:fresh")
    kd = _kit()
    assert "TEST_CMD='make test'" in (kd / "run.sh").read_text()
    scaffold = (kd / "specs" / "00-scaffold.md").read_text()
    assert "`Makefile` whose `test` target runs every suite" in scaffold
    assert "node: `npm test`" in scaffold and "python: `python -m pytest -q`" in scaffold


def test_new_target_falls_back_to_profile_stack(store, tmp_path):
    c, _ = _new_comp(store, tmp_path, "")
    kit.export_kit(store, c, "new:fresh")
    claude = (_kit() / "CLAUDE.md").read_text()
    assert "- python\n- fastapi" in claude and "`python -m pytest -q`" in claude


def test_dependency_policy_everywhere(store, tmp_path):
    c, _ = _new_comp(store, tmp_path, "Go")
    kit.export_kit(store, c, "new:fresh")
    kd = _kit()
    files = [kd / "CLAUDE.md", *sorted((kd / "specs").iterdir())]
    assert len(files) == 5
    for f in files:
        text = f.read_text()
        assert "no new third-party dependencies" not in text.lower(), f.name
        assert "DEPENDENCIES.md" in text, f.name
    assert "go test ./..." in (kd / "CLAUDE.md").read_text()


def test_acceptance_checks_in_specs(store, tmp_path, monkeypatch):
    c, caps = _new_comp(store, tmp_path, "Python")
    c.proposal.update(
        requirements=[{"id": "r1", "text": "configs parse"}, {"id": "r2", "text": "fast"}],
        coverage=[{"req": "r1", "kind": "catalog", "cap": f"c{caps[0].id}"},
                  {"req": "r2", "kind": "glue", "cap": ""}])
    c = store.save_composition(c)
    real = store.get_capability

    def with_checks(cid):
        cap = real(cid)
        if cap is not None and cap.name == "Parser":
            cap.acceptance = ["Given an empty file, parsing returns no records"]
        elif cap is not None:
            cap.acceptance = []
        return cap
    monkeypatch.setattr(store, "get_capability", with_checks)
    kit.export_kit(store, c, "new:fresh")
    specs = _kit() / "specs"
    parser = (specs / "01-parser.md").read_text()
    body = parser.split("**Acceptance checks:**\n\n")[1].split("\n\n")[0]
    assert body == ("- Given an empty file, parsing returns no records\n"
                    "- Covers requirement r1: configs parse")
    cache = (specs / "02-cache.md").read_text()
    assert "**Acceptance checks:**\n\n(none recorded: write a test for every behavior above " \
           "and list them in your step notes)" in cache
    gap = (specs / "03-fresh-auth-layer.md").read_text()
    assert "- The behavior above is implemented and covered by tests" in gap
    assert "Covers requirement r2" not in parser


def caps_id(store, name):
    return next(c.id for c in store.list_capabilities() if c.name == name)


def test_gap_order_and_glue_ids(store, tmp_path):
    caps = seed(store, tmp_path)
    store.upsert_dossier("x", "other", "https://github.com/x/other", "d", {
        "summary": "s", "dependencies": {"license": "MIT"},
        "capabilities": [{"name": "Scheduler", "what": "runs jobs", "portability": "high"}]})
    sched = caps_id(store, "Scheduler")
    parser, cache = caps[0].id, caps[1].id
    c = store.save_composition(Composition(
        slug="ordered", name="Ordered", target_kind="new", target_slug="fresh", proposal={
            "capabilities": [{"capability_id": parser, "order": 1},
                             {"capability_id": cache, "order": 2}],
            "stack": "Python",
            "glue": (f"c{parser}'s scheduler feeds c{cache}; see c{sched}. Keep src/c{parser}.py."
                     f" Both: c{parser}/c{cache}; unknown c99999 stays."),
            "gaps": [{"need": f"extend c{parser}", "build_fresh": True},
                     {"need": "store:cache for Cache", "token": "store:cache",
                      "consumer_capability_id": cache, "before_capability_id": cache,
                      "suggested_donors": ["a/b"], "build_fresh": True}]}))
    kit.export_kit(store, c, "new:fresh")
    kd = _kit()
    assert sorted(p.name for p in (kd / "specs").iterdir()) == [
        "00-scaffold.md", "01-parser.md", "02-fresh-store-cache-for-cache.md", "03-cache.md",
        f"04-fresh-extend-c{parser}.md"]
    bp = (kd / "BLUEPRINT.md").read_text()
    assert "Parser (step 1)'s scheduler feeds Cache (step 3); see Scheduler." in bp
    assert f"src/c{parser}.py" in bp
    assert "Both: Parser (step 1)/Cache (step 3); unknown c99999 stays." in bp
    assert "4. Step 4 — build fresh: extend Parser (step 1)" in bp
    wiring = (kd / "specs" / "02-fresh-store-cache-for-cache.md").read_text()
    assert wiring.startswith("## Build fresh: `store:cache` for Cache\n")
    assert "`store:cache`: A general-purpose cache of computed/fetched values" in wiring
    assert "Consumed by Cache (step 3): Caches results." in wiring
    assert "- Provides `store:cache` as defined above" in wiring
    assert "- Cache (step 3) can use this step's `store:cache` without a stub of its own" in wiring
    assert "**Suggested donors (comprehend later, do not read now):** `a/b`" in wiring
    fresh = (kd / "specs" / f"04-fresh-extend-c{parser}.md").read_text()
    assert "## Build fresh: extend Parser (step 1)" in fresh

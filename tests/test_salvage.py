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
    assert "natively in `thing` (python)" in spec  # new target: the profile stack
    assert "the suite runs with `python -m pytest -q`" in spec
    assert "Allowed dependencies: the stack below." in spec and "  - Stack: python" in spec
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


def test_policy_variants_and_ecosystems():
    assert salvage.split_stack("TypeScript + Next.js, SQLite (Drizzle)") == [
        "TypeScript", "Next.js", "SQLite (Drizzle)"]
    assert salvage.ecosystems(["TypeScript", "Next.js"]) == ["node"]
    assert salvage.ecosystems(["Django", "MongoDB"]) == ["python"]  # no "go" inside words
    assert salvage.ecosystems(["Go 1.22", "Node.js"]) == ["node", "go"]
    assert salvage.derive_test_cmd(["rust"]) == "cargo test"
    assert salvage.derive_test_cmd([]) == salvage.derive_test_cmd(["node", "go"]) == "make test"
    new = salvage.constraint_lines({"kind": "new", "stack": ["Go"], "test_cmd": "go test ./..."})
    existing = salvage.constraint_lines({"kind": "existing", "stack": ["python"],
                                         "test_cmd": "pytest -q", "donor": "a/b"})
    assert new[1] == ("- Allowed dependencies: the stack below. Any other dependency must be "
                      "small and necessary; before adding it, record its name, version range "
                      "and reason in `DEPENDENCIES.md`. Prefer the standard library.")
    assert new[2] == "  - Stack: Go" and "step 0" in new[4]
    assert new[3] == f"- {salvage.STACK_ITEM_DEPS}" and "a SQLite driver for Drizzle" in new[3]
    assert existing[1].startswith("- Allowed dependencies: the dependencies this project "
                                  "already uses. Any other")
    assert "`a/b`" in existing[0] and "existing conventions" in existing[3]
    assert all("no new third-party" not in x for x in new + existing)


def test_scaffold_spec():
    spec = salvage.build_scaffold_spec(["Python", "React"], "make test", ["node", "python"],
                                       target_name="new:app")
    assert spec.startswith("## Scaffold: new:app\n")
    for s in ["`package.json` (node), `pyproject.toml` (python)", "`Makefile`",
              "**Acceptance checks:**", "- `make test` exits 0 with at least one test per "
              "ecosystem (node, python)", "DEPENDENCIES.md", "  - Stack: Python, React"]:
        assert s in spec, s


def test_wiring_fresh_spec_without_store():
    spec = salvage.build_fresh_spec(
        {"need": "http:outbound-client for Widget", "token": "http:outbound-client"},
        target_slug="new:x")
    assert spec.startswith("## Build fresh: `http:outbound-client` for Widget\n")
    assert "`http:outbound-client`: " + salvage.NO_DEFINITION in spec
    assert "- Widget can use this step's `http:outbound-client` without a stub" in spec


def test_run_cli_target_kinds_and_stored_checks(store, tmp_path, monkeypatch):
    caps = seed(store, tmp_path)
    cap = caps[0]
    cap.acceptance = ["Given a bad file, parsing reports the line"]
    monkeypatch.setattr(store, "get_capability", lambda cid: cap)  # stored checks
    existing = salvage.run_cli(store, SimpleNamespace(capability_id=cap.id, into="proj"))["spec"]
    assert "the dependencies this project already uses" in existing
    assert "- Given a bad file, parsing reports the line" in existing
    new = salvage.run_cli(store, SimpleNamespace(capability_id=cap.id, into="new:z"))["spec"]
    assert "Allowed dependencies: the stack below." in new


def test_donor_paths_scrubbed_from_prose(store, tmp_path):
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.upsert_dossier("own", "paths", "https://github.com/own/paths", "p", {
        "summary": "s", "dependencies": {"license": "MIT"},
        "security": {"concerns": ["Pinger leaks tokens in AppPing.tsx:76"]},
        "capabilities": [{
            "name": "Pinger", "files": ["src/AppPing.tsx", "src/server/api/routers/app.ts"],
            "what": "Pings apps (see `src/server/api/routers/app.ts:41`). Built on Next.js.",
            "portability_notes": "Type guards in kanbanView.ts:55–84 and lib/util/guard.py "
                                 "are a good reference; config lives in package.json.",
            "security_notes": "Escape output as `AppPing.tsx` does."}]})
    cap = next(c for c in store.list_capabilities() if c.name == "Pinger")
    spec = salvage.build_clean_room_spec(store, cap, target_slug="new:x")
    prose, files_line = spec.split("**Donor reference files")
    for leak in ["kanbanView", "app.ts", "AppPing", "guard.py", ":41", "55–84"]:
        assert leak not in prose, leak
    assert prose.count(salvage.DONOR_FILE) == 5
    assert "Built on Next.js." in prose and "package.json" in prose  # prose, not paths
    assert "`src/AppPing.tsx`" in files_line and "`src/server/api/routers/app.ts`" in files_line


def test_fresh_spec_cites_weak_candidates():
    spec = salvage.build_fresh_spec(
        {"need": "data:feed for Reader", "token": "data:feed",
         "candidates": [{"capability_id": 3, "name": "Ledger", "donor": "o/b",
                         "relevance": 0.12}]}, target_slug="new:x")
    assert ("**Catalog parts that also provide this (weak matches, not used):**\n\n"
            "- Ledger (o/b, relevance 0.12)") in spec

"""Batch 5 kit honesty: refusals, target policy, re-export safety, atomic writes, wiring order."""

import json
import re
import zipfile
from types import SimpleNamespace

import pytest

from reincarnate import kit, paths, salvage
from reincarnate.models import Composition, Profile, Project


def _seed(store, parts):
    """parts: (name, provides, requires) -> {name: capability id}."""
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", {
        "summary": "A tool.", "dependencies": {"license": "MIT"},
        "capabilities": [{"name": n, "what": f"{n} does its work.", "portability": "high",
                          "files": ["src/a.py"], "provides": list(p), "requires": list(r)}
                         for n, p, r in parts]})
    return {c.name: c.id for c in store.list_capabilities()}


PLAIN = [("Alpha", [], []), ("Beta", [], [])]


def _comp(store, ids, names=None, *, rows=None, slug="app", target_kind="new",
          target_slug="fresh", **extra):
    names = names or list(ids)
    proposal = {"capabilities": rows or [{"capability_id": ids[n], "order": i, "name": n,
                                          "donor": "own/repo"}
                                         for i, n in enumerate(names, 1)],
                "stack": "Python", "gaps": []}
    proposal.update(extra)
    return store.save_composition(Composition(
        slug=slug, name="App", target_kind=target_kind, target_slug=target_slug,
        proposal=proposal))


def _kd(name="fresh"):
    return paths.data_dir() / "new" / name / ".reincarnate"


def _project(tmp_path, name="proj", manifest="pyproject.toml"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / manifest).write_text("x\n")
    return d


# -- 1. missing or substituted parts ------------------------------------------------------
def test_missing_retired_and_substituted_parts_refuse(store):
    ids = _seed(store, PLAIN + [("Gamma", [], [])])
    store.conn.execute("UPDATE capabilities SET retired_at='2026-01-01' WHERE id=?",
                       (ids["Beta"],))
    rows = [{"capability_id": ids["Alpha"], "order": 1, "name": "Not Alpha", "donor": "own/repo"},
            {"capability_id": ids["Beta"], "order": 2, "name": "Beta", "donor": "own/repo"},
            {"capability_id": 9999, "order": 3, "name": "Ghost", "donor": "x/y"},
            {"capability_id": ids["Gamma"], "order": 4, "name": "Gamma", "donor": "own/repo"}]
    c = _comp(store, ids, rows=rows, problems=["fewer than 2 valid capabilities"])
    with pytest.raises(ValueError) as e:
        kit.export_kit(store, c, "new:fresh")
    msg = str(e.value)
    assert "is now a different part (Alpha from own/repo)" in msg
    assert "part c%d (Beta) is retired" % ids["Beta"] in msg
    assert "part c9999 (Ghost) is missing" in msg
    assert not _kd().exists()
    r = kit.export_kit(store, c, "new:fresh", allow_missing=True)
    assert r["steps"] == 2  # scaffold + Gamma
    assert any("Ghost" in w for w in r["warnings"])
    assert "fewer than 2 valid capabilities" in r["warnings"]
    bp = (_kd() / "BLUEPRINT.md").read_text()
    assert "## Problems" in bp and "Ghost" in bp and "fewer than 2 valid capabilities" in bp


def test_donor_mismatch_and_old_rows_without_names(store):
    ids = _seed(store, PLAIN)
    rows = [{"capability_id": ids["Alpha"], "order": 1, "name": " alpha. ", "donor": "OWN/Repo"},
            {"capability_id": ids["Beta"], "order": 2}]  # pre-batch-5 row: nothing to compare
    assert kit.export_kit(store, _comp(store, ids, rows=rows), "new:fresh")["steps"] == 3
    rows[0]["donor"] = "other/repo"
    with pytest.raises(ValueError, match="different part"):
        kit.export_kit(store, _comp(store, ids, rows=rows, slug="two"), "new:fresh-two")


def test_compose_rows_record_name_and_donor(store):
    from reincarnate import compose
    ids = _seed(store, PLAIN)
    rows = [{"capability_id": i, "name": n, "donor": "own/repo", "license": "MIT",
             "portability": "high"} for n, i in ids.items()]
    raw = {"capabilities": [{"capability_id": i} for i in ids.values()]}
    proposal, _ = compose.validate_proposal(store, raw, rows, None, target="new:x")
    assert {(r["name"], r["donor"]) for r in proposal["capabilities"]} == {
        ("Alpha", "own/repo"), ("Beta", "own/repo")}


# -- 2. target decides new vs existing ----------------------------------------------------
def test_registered_project_is_existing_even_when_composition_said_new(store, tmp_path):
    ids = _seed(store, PLAIN)
    d = _project(tmp_path)
    store.add_project(Project(slug="proj", path=str(d), stack=["rust"], test_cmd="cargo t"))
    c = _comp(store, ids, target_kind="new")
    r = kit.export_kit(store, c, "proj")
    assert not (d / ".reincarnate" / "specs" / "00-scaffold.md").exists() and r["steps"] == 2
    assert "- rust" in (d / ".reincarnate" / "CLAUDE.md").read_text()
    assert any("designed for a new project" in w and "proj" in w for w in r["warnings"])


def test_directory_with_go_mod_infers_go(store, tmp_path):
    ids = _seed(store, PLAIN)
    d = _project(tmp_path, "goapp", "go.mod")
    r = kit.export_kit(store, _comp(store, ids, target_kind="new"), str(d))
    claude = (d / ".reincarnate" / "CLAUDE.md").read_text()
    assert "- go\n" in claude and "`go test ./...`" in claude
    assert r["steps"] == 2


def test_directory_without_manifest_is_new(store, tmp_path):
    ids = _seed(store, PLAIN)
    d = tmp_path / "empty"
    d.mkdir()
    r = kit.export_kit(store, _comp(store, ids, target_kind="existing"), str(d))
    assert (d / ".reincarnate" / "specs" / "00-scaffold.md").is_file()
    assert any("existing project" in w for w in r["warnings"])


def test_into_dot_is_absolute(store, tmp_path, monkeypatch):
    ids = _seed(store, PLAIN)
    d = _project(tmp_path, "here")
    monkeypatch.chdir(d)
    r = kit.export_kit(store, _comp(store, ids), ".")
    assert r["kit_dir"] == str((d / ".reincarnate").resolve())
    assert r["target"] == "here"


# -- 3. --notes drive glue ----------------------------------------------------------------
def test_notes_drive_glue_steps(store, tmp_path):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids, glue="- nothing useful",
              requirements=[{"id": "r1", "text": "Connect the alpha feed to the beta cache"}],
              coverage=[{"req": "r1", "kind": "glue", "cap": ""}])
    notes = "- Connect the alpha feed to the beta cache: Alpha pushes into Beta on each tick.\n"
    kit.export_kit(store, c, "new:fresh", notes=notes)
    kd = _kd()
    glue = next(p for p in (kd / "specs").iterdir() if "-glue-" in p.name).read_text()
    assert "Alpha pushes into Beta on each tick" in glue and "nothing useful" not in glue
    assert "Alpha (step 1)" in glue and "Beta (step 2)" in glue
    bp = (kd / "BLUEPRINT.md").read_text()
    assert "Alpha pushes into Beta" in bp.split("## Glue")[1]
    assert "How the parts connect is in BLUEPRINT.md, section Glue." in (
        kd / "CLAUDE.md").read_text()


# -- 4. gap steps before auth -------------------------------------------------------------
def test_http_gap_before_auth_gets_note_and_auth_check(store):
    ids = _seed(store, [("Pages", ["ui:pages"], []), ("Login", ["auth:user-identity"], [])])
    c = _comp(store, ids, names=["Pages", "Login"],
              gaps=[{"need": "Board endpoint", "token": "http:board",
                     "before_capability_id": ids["Pages"]}])
    # drop the ui: part's own note by making Pages a non-surface part
    store.set_capability_fields(ids["Pages"], provides=["data:page"])
    kit.export_kit(store, c, "new:fresh")
    specs = {p.name: p.read_text() for p in (_kd() / "specs").iterdir()}
    fresh = next(v for k, v in specs.items() if "-fresh-" in k)
    assert "Cross-cutting: auth arrives in step 3 (Login)" in fresh
    login = next(v for k, v in specs.items() if "login" in k)
    assert "An anonymous request to an endpoint added before this step is rejected" in login


# -- 5. re-export keeps progress and edits ------------------------------------------------
def _tick(kd, n, suffix=""):
    t = (kd / "tasks.md").read_text().splitlines()
    t = [re.sub(rf"^- \[ \] (Step {n}:.*)$", rf"- [x] \1{suffix}", ln) for ln in t]
    (kd / "tasks.md").write_text("\n".join(t) + "\n")


def test_reexport_refuses_ticks_then_force_backs_up_and_carries(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kit.export_kit(store, c, "new:fresh")
    kd = _kd()
    manifest = json.loads((kd / "manifest.json").read_text())
    assert manifest["composition"] == "app" and manifest["steps"][1]["n"] == 1
    assert set(manifest["files"]) >= {"BLUEPRINT.md", "CLAUDE.md", "run.sh"}
    _tick(kd, 0)
    _tick(kd, 1, " (unverified: Code Read not installed)")
    (kd / "state.json").write_text("{}")
    (kd / "checks").mkdir()
    (kd / "checks" / "step-01.txt").write_text("ok")
    with pytest.raises(ValueError, match="--force"):
        kit.export_kit(store, c, "new:fresh")
    assert "[x] Step 1" in (kd / "tasks.md").read_text()  # untouched
    r = kit.export_kit(store, c, "new:fresh", force=True)
    tasks = (kd / "tasks.md").read_text()
    assert "- [x] Step 0: scaffold the project" in tasks
    assert "(from own/repo) (unverified: Code Read not installed)" in tasks
    assert "- [ ] Step 2:" in tasks
    backups = list(kd.glob("backup-*"))
    assert len(backups) == 1 and (backups[0] / "tasks.md").is_file()
    assert "[x] Step 1" in (backups[0] / "tasks.md").read_text()
    assert any("backed up" in w for w in r["warnings"])
    assert (kd / "state.json").is_file() and (kd / "checks" / "step-01.txt").is_file()
    # nothing is ticked beyond what carried over, so a clean re-export of an untouched kit is fine
    kit.export_kit(store, c, "new:fresh", force=True)


def test_reexport_refuses_hand_edited_spec_and_leaves_unknown_files(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kit.export_kit(store, c, "new:fresh")
    kd = _kd()
    kit.export_kit(store, c, "new:fresh")  # untouched kit: allowed
    (kd / "specs" / "notes.md").write_text("mine")
    spec = next(p for p in (kd / "specs").iterdir() if p.name.startswith("01-"))
    spec.write_text(spec.read_text() + "\nmy edit\n")
    with pytest.raises(ValueError, match=r"edited by hand: specs/01-"):
        kit.export_kit(store, c, "new:fresh")
    r = kit.export_kit(store, c, "new:fresh", force=True)
    assert "my edit" not in spec.read_text() and "my edit" in next(
        kd.glob("backup-*/specs/01-*")).read_text()
    assert (kd / "specs" / "notes.md").read_text() == "mine"
    assert any("notes.md" in w for w in r["warnings"])


def test_reexport_deletes_only_listed_obsolete_specs(store):
    ids = _seed(store, PLAIN)
    kit.export_kit(store, _comp(store, ids), "new:fresh")
    kd = _kd()
    (kd / "specs" / "zz-mine.txt").write_text("keep")
    kit.export_kit(store, _comp(store, ids, names=["Beta"], slug="app"), "new:fresh")
    names = sorted(p.name for p in (kd / "specs").iterdir())
    assert names == ["00-scaffold.md", "01-beta.md", "zz-mine.txt"]


def test_kit_without_manifest_and_ticks_needs_force(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kd = _kd()
    (kd / "specs").mkdir(parents=True)
    (kd / "specs" / "01-old.md").write_text("old")
    (kd / "tasks.md").write_text("- [x] Step 1: Alpha (from own/repo)\n  spec: x\n")
    with pytest.raises(ValueError, match="--force"):
        kit.export_kit(store, c, "new:fresh")
    kit.export_kit(store, c, "new:fresh", force=True)
    assert not (kd / "specs" / "01-old.md").exists()
    assert (kd / "tasks.md").read_text().count("- [x]") == 1
    assert next(kd.glob("backup-*/specs/01-old.md")).read_text() == "old"


# -- 6. atomic and safe writes ------------------------------------------------------------
def test_long_names_are_capped(store):
    ids = _seed(store, [("Z" * 300, [], []), ("Beta", [], [])])
    kit.export_kit(store, _comp(store, ids), "new:fresh")
    for p in (_kd() / "specs").iterdir():
        assert len(p.name) <= 3 + 40 + 3 + 6


def test_write_failure_leaves_old_kit_intact(store, monkeypatch):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kit.export_kit(store, c, "new:fresh")
    kd = _kd()
    before = {p.name: p.read_text() for p in kd.rglob("*") if p.is_file()}
    real, calls = kit._stage, []

    def flaky(tmp, text, executable):
        calls.append(tmp)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        real(tmp, text, executable)

    monkeypatch.setattr(kit, "_stage", flaky)
    c.proposal["risks"] = ["a new risk"]
    store.save_composition(c)
    r = kit.run_cli(store, SimpleNamespace(composition=c.slug, into="new:fresh"))
    assert "No space left" in r["error"] and "\n" not in r["error"]
    assert {p.name: p.read_text() for p in kd.rglob("*") if p.is_file()} == before


def test_symlinked_kit_file_is_refused(store, tmp_path):
    ids = _seed(store, PLAIN)
    d = _project(tmp_path)
    store.add_project(Project(slug="proj", path=str(d), stack=["python"]))
    victim = d / "CLAUDE.md"
    victim.write_text("project instructions")
    (d / ".reincarnate").mkdir()
    (d / ".reincarnate" / "CLAUDE.md").symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        kit.export_kit(store, _comp(store, ids), "proj")
    assert victim.read_text() == "project instructions"
    (d / ".reincarnate" / "CLAUDE.md").unlink()
    (d / ".reincarnate").rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (d / ".reincarnate").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symlink"):
        kit.export_kit(store, _comp(store, ids), "proj")
    assert list(elsewhere.iterdir()) == []


# -- 7. names ----------------------------------------------------------------------------
def test_unicode_names_and_suffix_for_other_compositions(store):
    assert kit.slugify("Приложение 日本 App!") == "приложение-日本-app"
    assert kit.slugify("!!!") == "app"
    ids = _seed(store, PLAIN)
    r = kit.export_kit(store, _comp(store, ids, slug="one"), "new:Café")
    assert r["kit_dir"].endswith("/new/café/.reincarnate")
    same = kit.export_kit(store, _comp(store, ids, slug="one"), "new:Café")
    assert same["kit_dir"] == r["kit_dir"]
    assert not any("already holds" in w for w in same["warnings"])
    other = kit.export_kit(store, _comp(store, ids, slug="two"), "new:Café")
    assert other["kit_dir"].endswith("/new/café-2/.reincarnate")
    assert any("café-2" in w for w in other["warnings"])
    third = kit.export_kit(store, _comp(store, ids, slug="three"), "new:Café")
    assert third["kit_dir"].endswith("/new/café-3/.reincarnate")


# -- 8. stack splitting ------------------------------------------------------------------
def test_split_stack_respects_parentheses_and_lists():
    assert salvage.split_stack("Python + FastAPI (with Pydantic, uvicorn) + SQLite") == [
        "Python", "FastAPI (with Pydantic, uvicorn)", "SQLite"]
    assert salvage.split_stack("a, b [x + y], c") == ["a", "b [x + y]", "c"]
    assert salvage.split_stack(["Python", " Go ", ""]) == ["Python", "Go"]
    assert salvage.split_stack(None) == []


def test_stack_bullets_in_claude_md(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    c.proposal["stack"] = "Python + FastAPI (with Pydantic, uvicorn) + SQLite"
    store.save_composition(c)
    kit.export_kit(store, c, "new:fresh")
    claude = (_kd() / "CLAUDE.md").read_text()
    assert "- FastAPI (with Pydantic, uvicorn)\n- SQLite\n" in claude
    c.proposal["stack"] = ["Go", "SQLite"]
    store.save_composition(c)
    kit.export_kit(store, c, "new:fresh", force=True)
    claude = (_kd() / "CLAUDE.md").read_text()
    assert "- Go\n- SQLite\n" in claude and "['" not in claude


# -- 9. wiring order ---------------------------------------------------------------------
def test_providers_come_before_consumers(store):
    ids = _seed(store, [("Consumer", [], ["data:feed", "config:x"]),
                        ("Provider", ["data:feed"], []),
                        ("Loop A", ["data:a"], ["data:b"]), ("Loop B", ["data:b"], ["data:a"])])
    c = _comp(store, ids, names=["Consumer", "Loop A", "Loop B", "Provider"])
    kit.export_kit(store, c, "new:fresh")
    names = sorted(p.name for p in (_kd() / "specs").iterdir())
    # the cycle (Loop A / Loop B) keeps its proposal order, after what could be placed
    assert names == ["00-scaffold.md", "01-provider.md", "02-consumer.md", "03-loop-a.md",
                     "04-loop-b.md"]
    consumer = (_kd() / "specs" / "02-consumer.md").read_text()
    assert "**Needs from earlier steps:** data:feed from Provider (step 1)" in consumer
    assert "config:x" not in consumer.split("Needs from earlier steps")[1].split("\n")[0]
    assert "Needs from earlier steps" not in (_kd() / "specs" / "01-provider.md").read_text()


def test_design_prompt_asks_for_foundations_first():
    from reincarnate import design
    assert "foundational parts" in design.compose_prompt("an app", 3)


# -- 10. zip and kits table --------------------------------------------------------------
def test_zip_holds_only_listed_files(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kit.export_kit(store, c, "new:fresh")
    kd = _kd()
    (kd / "state.json").write_text("{}")
    (kd / "checks").mkdir()
    (kd / "checks" / "s.txt").write_text("x")
    (kd / "specs" / "stray.md").write_text("stray")
    (kd / "backup-20200101T000000Z").mkdir()
    (kd / "backup-20200101T000000Z" / "tasks.md").write_text("old")
    r = kit.export_kit(store, c, "new:fresh", zip_it=True)
    with zipfile.ZipFile(r["zip"]) as z:
        names = sorted(z.namelist())
    assert names == sorted(f".reincarnate/{f}" for f in r["files"])
    assert ".reincarnate/manifest.json" in names and ".reincarnate/tasks.md" in names
    assert not any("state.json" in n or "stray" in n or "backup" in n or "checks" in n
                   for n in names)


def test_add_kit_updates_the_existing_row(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kit.export_kit(store, c, "new:fresh")
    kit.export_kit(store, c, "new:fresh")
    assert store.conn.execute("SELECT COUNT(*) FROM kits").fetchone()[0] == 1
    other = _comp(store, ids, slug="other")
    kit.export_kit(store, other, "new:fresh-other")
    assert store.conn.execute("SELECT COUNT(*) FROM kits").fetchone()[0] == 2


@pytest.mark.parametrize("with_manifest", [True, False])
def test_project_scaffolded_by_its_kit_stays_new_on_reexport(store, tmp_path, with_manifest):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids, stack="TypeScript + Next.js (App Router) + SQLite")
    d = tmp_path / "built"
    d.mkdir()
    kit.export_kit(store, c, str(d))
    kd = d / ".reincarnate"
    assert json.loads((kd / "manifest.json").read_text())["kind"] == "new"
    (d / "package.json").write_text("{}\n")  # step 0 wrote the project's manifests
    _tick(kd, 0)
    if not with_manifest:  # a kit exported before manifests existed
        (kd / "manifest.json").unlink()
    r = kit.export_kit(store, c, str(d), force=True)
    tasks = (kd / "tasks.md").read_text()
    assert "- [x] Step 0: scaffold the project" in tasks
    assert "Next.js (App Router)" in (kd / "CLAUDE.md").read_text()
    assert not any("keeps its stack" in w for w in r["warnings"])
    # another composition exported into the same directory sees an existing project
    c2 = _comp(store, ids, slug="other", stack="Python")
    r2 = kit.export_kit(store, c2, str(d), force=True)
    assert r2["steps"] == 2 and any("keeps its stack" in w for w in r2["warnings"])


# -- dry run (P2) ---------------------------------------------------------------------------
def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else None


def test_dry_run_renders_what_a_real_export_writes_and_writes_nothing(store):
    ids = _seed(store, PLAIN + [("Gamma", ["http:api"], ["store:x"])])
    c = _comp(store, ids, gaps=[{"need": "billing", "suggested_donors": [], "build_fresh": True}])
    kits_before = store.conn.execute("SELECT COUNT(*) FROM kits").fetchone()[0]
    out = kit.export_kit(store, c, "new:fresh", dry_run=True)
    assert out["refusal"] == []
    assert not _kd().exists() and not (paths.data_dir() / "new").exists()
    assert store.conn.execute("SELECT COUNT(*) FROM kits").fetchone()[0] == kits_before
    assert store.get_composition("app").status == "proposed"
    assert out["target"]["kind"] == "new" and out["target"]["dir"].endswith("new/fresh")
    assert out["target"]["stack"] and "test_cmd" in out["target"]

    real = kit.export_kit(store, store.get_composition("app"), "new:fresh")
    assert real["steps"] == len(out["steps"])
    for rel, text in out["files"].items():
        if rel == "manifest.json":
            continue
        assert (_kd() / rel).read_text() == text, rel
    assert set(out["files"]) == {p.relative_to(_kd()).as_posix() for p in _kd().rglob("*")
                                 if p.is_file()}
    assert (_kd() / "run.sh").read_text() == out["files"]["run.sh"]
    # the step list matches the written tasks
    tasks = (_kd() / "tasks.md").read_text()
    for st in out["steps"]:
        assert f"- [ ] Step {st['n']}: {st['title']}" in tasks


def test_dry_run_step_records(store):
    ids = _seed(store, [("Auth", ["auth:session"], []), ("Dash", ["ui:dashboard"],
                                                         ["auth:session"])])
    store.conn.execute("UPDATE capabilities SET deepened_at='2026-01-01' WHERE id=?",
                       (ids["Auth"],))
    c = _comp(store, ids)
    out = kit.export_kit(store, c, "new:fresh", dry_run=True)
    steps = {st["title"]: st for st in out["steps"]}
    assert [st["n"] for st in out["steps"]] == [0, 1, 2]
    scaffold = out["steps"][0]
    assert scaffold["kind"] == "scaffold" and scaffold["deepened"] is False
    auth, dash = out["steps"][1], out["steps"][2]
    assert auth["kind"] == "cap" and auth["donor"] == "own/repo" and auth["deepened"] is True
    assert dash["deepened"] is False
    assert auth["guard_note"] is True and dash["guard_note"] is False  # the guard lands in step 1
    assert scaffold["guard_note"] is False
    assert dash["needs"] == ["auth:session from Auth (step 1)"]
    assert auth["needs"] == [] and steps[auth["title"]] is auth
    for st in out["steps"]:
        assert isinstance(st["checks"], int) and st["spec_head"]
        assert len(st["spec_head"].splitlines()) <= 40
    assert any(st["checks"] >= 1 for st in out["steps"])


def test_dry_run_reports_refusals_instead_of_raising(store):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    kit.export_kit(store, c, "new:fresh")
    t = _kd() / "tasks.md"
    t.write_text(t.read_text().replace("- [ ] Step 1:", "- [x] Step 1:"))
    spec = next((_kd() / "specs").glob("01-*.md"))
    spec.write_text(spec.read_text() + "\nmy edit\n")
    before = _tree(_kd().parent)
    texts = {p: p.read_text() for p in _kd().rglob("*") if p.is_file()}
    out = kit.export_kit(store, c, "new:fresh", dry_run=True)
    assert len(out["refusal"]) == 1
    assert "ticked step" in out["refusal"][0] and "edited by hand" in out["refusal"][0]
    assert out["steps"] and out["files"]  # still rendered
    with pytest.raises(ValueError, match="progress or edits"):
        kit.export_kit(store, c, "new:fresh")
    forced = kit.export_kit(store, c, "new:fresh", dry_run=True, force=True)
    assert forced["refusal"] == [] and "would be backed up" in " ".join(forced["warnings"])
    assert _tree(_kd().parent) == before  # no backup dir either
    assert {p: p.read_text() for p in _kd().rglob("*") if p.is_file()} == texts


def test_dry_run_reports_missing_parts_and_unknown_targets(store):
    ids = _seed(store, PLAIN)
    rows = [{"capability_id": ids["Alpha"], "order": 1, "name": "Alpha", "donor": "own/repo"},
            {"capability_id": 9999, "order": 2, "name": "Ghost", "donor": "x/y"}]
    c = _comp(store, ids, rows=rows)
    out = kit.export_kit(store, c, "new:fresh", dry_run=True)
    assert len(out["refusal"]) == 1 and "Ghost" in out["refusal"][0]
    assert out["steps"]
    none = kit.export_kit(store, c, "nowhere-at-all", dry_run=True)
    assert "unknown target" in none["refusal"][0] and none["steps"] == []
    empty = _comp(store, ids, rows=[{"capability_id": 9999, "order": 1}], slug="gone")
    got = kit.export_kit(store, empty, "new:x", dry_run=True)
    assert any("no buildable steps" in r for r in got["refusal"]) and got["steps"] == []
    assert not _kd().exists()


def test_dry_run_flags_symlinks(store, tmp_path):
    ids = _seed(store, PLAIN)
    c = _comp(store, ids)
    d = _project(tmp_path)
    (d / ".reincarnate").mkdir()
    (d / ".reincarnate" / "CLAUDE.md").symlink_to(tmp_path / "elsewhere")
    out = kit.export_kit(store, c, str(d), dry_run=True)
    assert any("symlink" in r for r in out["refusal"])
    assert out["target"]["kind"] == "existing"


def test_dry_run_never_deepens(store, monkeypatch):
    ids = _seed(store, PLAIN)
    monkeypatch.setattr(kit, "_run_deepen", lambda *a, **k: pytest.fail("deepen ran"))
    kit.export_kit(store, _comp(store, ids), "new:fresh", dry_run=True, deepen=True)


def test_export_progress_hook_fires_after_the_deepen_pass(store, monkeypatch):
    ids = _seed(store, PLAIN)
    seen = []
    monkeypatch.setattr(kit, "_run_deepen", lambda s, i: seen.append("deepen") or {})
    kit.export_kit(store, _comp(store, ids), "new:fresh", deepen=True,
                   progress=lambda: seen.append("write"))
    assert seen == ["deepen", "write"]

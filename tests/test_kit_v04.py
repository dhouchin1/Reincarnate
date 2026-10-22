"""v0.4 kit generation: UI test tooling, dependency sentence, data shapes, deepen, auth guard
seam, glue steps (DESIGN "Kit generation")."""

import re
import sys
import types
from types import SimpleNamespace

import pytest

from reincarnate import kit, paths, salvage
from reincarnate.models import Composition, Profile, Project

PARTS = [
    ("Board API", "serves board tiles over http", ["http:board-api"]),
    ("Board view", "draws the dashboard grid", ["ui:dashboard"]),
    ("Login", "signs users in with passwords", ["auth:user-identity"]),
    ("Tile pinger", "pings each tile service", ["ui:status-dot"]),
]


def _seed(store, parts=PARTS):
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", {
        "summary": "A tool.", "dependencies": {"license": "MIT"},
        "capabilities": [{"name": n, "what": w, "portability": "high", "files": ["src/a.py"],
                          "provides": list(p)} for n, w, p in parts]})
    return {c.name: c.id for c in store.list_capabilities()}


def _comp(store, ids, *, stack="TypeScript + React", names=None, target_kind="new",
          target_slug="fresh", **extra):
    names = names or [n for n, _, _ in PARTS]
    proposal = {"capabilities": [{"capability_id": ids[n], "order": i}
                                 for i, n in enumerate(names, 1)],
                "stack": stack, "gaps": []}
    proposal.update(extra)
    return store.save_composition(Composition(
        slug="v4-app", name="V4", target_kind=target_kind, target_slug=target_slug,
        proposal=proposal))


def _kd(name="fresh"):
    return paths.data_dir() / "new" / name / ".reincarnate"


def _section(text, head):
    return text.split(f"**{head}:**\n\n")[1].split("\n\n")[0].strip()


def _specs(kd):
    return {p.name: p.read_text() for p in sorted((kd / "specs").iterdir())}


# -- UI test tooling ----------------------------------------------------------------------
@pytest.mark.parametrize("stack", ["TypeScript + React", "TypeScript + Next.js + tRPC"])
def test_ui_framework_gets_component_tests(store, stack):
    ids = _seed(store)
    kit.export_kit(store, _comp(store, ids, stack=stack), "new:fresh")
    kd = _kd()
    scaffold = (kd / "specs" / "00-scaffold.md").read_text()
    assert "`@testing-library/react` with `jsdom`" in scaffold
    assert f"- {salvage.COMPONENT_SMOKE_CHECK}" in _section(scaffold, "Acceptance checks")
    claude = (kd / "CLAUDE.md").read_text()
    tests = claude.split("## Tests\n\n")[1].split("\n## ")[0]
    assert "Component tests: `@testing-library/react` with `jsdom`" in tests
    specs = _specs(kd)
    view = _section(specs["02-board-view.md"], "Acceptance checks")  # provides ui:dashboard
    assert f"- {salvage.COMPONENT_CHECK}" in view
    api = _section(specs["01-board-api.md"], "Acceptance checks")  # http: only
    assert salvage.COMPONENT_CHECK not in api


def test_python_stack_has_no_ui_tooling(store):
    ids = _seed(store)
    kit.export_kit(store, _comp(store, ids, stack="Python + FastAPI"), "new:fresh")
    kd = _kd()
    for name, text in _specs(kd).items():
        assert "jsdom" not in text and salvage.COMPONENT_CHECK not in text, name
        assert salvage.COMPONENT_SMOKE_CHECK not in text, name
    assert "Component tests" not in (kd / "CLAUDE.md").read_text()


def test_ui_test_tools_mapping():
    assert salvage.ui_test_tools(["Vue", "Nuxt"]) == ["@vue/test-utils", "jsdom"]
    assert salvage.ui_test_tools(["SvelteKit"]) == ["@testing-library/svelte", "jsdom"]
    assert salvage.ui_test_tools(["Solid"]) == ["@solidjs/testing-library", "jsdom"]
    assert salvage.ui_test_tools(["Python", "FastAPI"]) == []
    assert salvage.ecosystems(["Nuxt"]) == ["node"]


# -- dependency policy --------------------------------------------------------------------
def test_stack_item_sentence_everywhere(store):
    ids = _seed(store)
    c = _comp(store, ids, gaps=[{"need": "audit log", "build_fresh": True}],
              requirements=[{"id": "r1", "text": "nightly export"}],
              coverage=[{"req": "r1", "kind": "glue", "cap": ""}])
    kit.export_kit(store, c, "new:fresh")
    kd = _kd()
    files = [kd / "CLAUDE.md", *sorted((kd / "specs").iterdir())]
    assert len(files) == 8  # CLAUDE.md, scaffold, 4 parts, 1 glue, 1 gap
    for f in files:
        assert salvage.STACK_ITEM_DEPS in f.read_text(), f.name
    assert "a SQLite driver for Drizzle" in salvage.STACK_ITEM_DEPS


# -- data shapes, key values, deepen ------------------------------------------------------
SHAPES = [{"name": "Board", "notes": "",
           "fields": [{"name": "apps", "type": "list of App",
                       "meaning": "tiles on the board, see src/board/model.ts:12"},
                      {"name": "name", "type": "string", "meaning": "unique board name"}]}]
KEYS = [{"name": "md breakpoint", "value": "768px", "meaning": "viewport width where md starts"}]


def _with_fields(store, monkeypatch, deep=("Board API",), shaped=("Board API",)):
    real = store.get_capability

    def get(cid):
        cap = real(cid)
        if cap is not None:
            cap.data_shapes = SHAPES if cap.name in shaped else []
            cap.key_values = KEYS if cap.name in shaped else []
            cap.deepened_at = "2026-10-22T00:00:00Z" if cap.name in deep else ""
        return cap
    monkeypatch.setattr(store, "get_capability", get)


def test_specs_render_shapes_and_key_values(store, monkeypatch):
    ids = _seed(store)
    _with_fields(store, monkeypatch)
    r = kit.export_kit(store, _comp(store, ids, stack="Python"), "new:fresh")
    specs = _specs(_kd())
    api = specs["01-board-api.md"]
    assert ("**Data shapes:**\n\nBoard\n- apps (list of App): tiles on the board, see "
            f"{salvage.DONOR_FILE}\n- name (string): unique board name\n\n**Key values:**\n\n"
            "- md breakpoint: 768px — viewport width where md starts\n") in api
    assert "model.ts" not in api
    behavior = api.split("**Behavior to implement:**")[1]
    assert behavior.index("serves board tiles") < behavior.index("**Data shapes:**")
    view = specs["02-board-view.md"]
    assert salvage.NO_SHAPES in view and "**Key values:**" not in view
    assert r["warnings"] == ["3 parts are not deepened: reincarnate deepen --composition v4-app"]
    assert "deepen" not in r


def test_kit_deepen_runs_fake_deepen_for_not_deepened(store, monkeypatch):
    ids = _seed(store)
    _with_fields(store, monkeypatch, deep=("Board API", "Login"))
    calls = []
    fake = types.ModuleType("reincarnate.deepen")

    def deepen(store_, capability_ids, *, model, max_turns, force):
        calls.append((list(capability_ids), model, max_turns, force))
        return {"capabilities": list(capability_ids), "deepened": list(capability_ids),
                "skipped": [], "failed": [], "cost_usd": 0.01, "text": "2 deepened"}
    fake.deepen = deepen
    monkeypatch.setitem(sys.modules, "reincarnate.deepen", fake)
    c = _comp(store, ids, stack="Python")
    r = kit.run_cli(store, SimpleNamespace(composition=c.slug, into="new:fresh", zip=False,
                                           deepen=True))
    assert calls == [([ids["Board view"], ids["Tile pinger"]], kit.DEEPEN_MODEL, 25, False)]
    assert r["deepen"]["text"] == "2 deepened" and r["deepen"]["cost_usd"] == 0.01
    # the fake did not change the store, so the two parts still count
    assert r["warnings"] == ["2 parts are not deepened: reincarnate deepen --composition v4-app"]
    calls.clear()
    r = kit.run_cli(store, SimpleNamespace(composition=c.slug, into="new:fresh", zip=False))
    assert calls == [] and "deepen" not in r


# -- auth guard seam ----------------------------------------------------------------------
NOTE = "**Cross-cutting: auth arrives in step 3 (Login).**"


def test_auth_guard_seam_new_target(store):
    ids = _seed(store)
    kit.export_kit(store, _comp(store, ids, stack="Python"), "new:fresh")
    specs = _specs(_kd())
    scaffold = specs["00-scaffold.md"]
    assert "One authorization guard: a single function every endpoint and edit action calls. " \
           "It allows everything until step 3 (Login)" in scaffold
    assert "- A test shows the authorization guard currently allows every request" in scaffold
    for f in ("01-board-api.md", "02-board-view.md"):
        assert (f"{NOTE} Put every endpoint and edit action this step adds behind the "
                "authorization guard (create it if it does not exist). It allows everything "
                "until step 3.") in specs[f], f
        text = specs[f]
        assert text.index(NOTE) < text.index("**Acceptance checks:**")
    login = specs["03-login.md"]
    assert NOTE not in login
    assert ("Wire the authorization guard to this step's identity and roles so every endpoint "
            "added before this step is gated") in login
    assert "- An anonymous request to an endpoint added before this step is rejected" in \
        _section(login, "Acceptance checks")
    assert "Cross-cutting" not in specs["04-tile-pinger.md"]  # ui:, but after step 3


def test_auth_notes_existing_target(store, tmp_path):
    ids = _seed(store)
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "package.json").write_text("{}")
    store.add_project(Project(slug="proj", path=str(tmp_path / "proj"), stack=["React"],
                              test_cmd="npm test"))
    c = _comp(store, ids, target_kind="project", target_slug="proj")
    kit.export_kit(store, c, "proj")
    specs = _specs(tmp_path / "proj" / ".reincarnate")
    assert "00-scaffold.md" not in specs and "01-board-api.md" in specs
    note = "behind the authorization guard (create it if it does not exist)"
    assert NOTE in specs["01-board-api.md"] and note in specs["01-board-api.md"]
    assert NOTE in specs["02-board-view.md"]
    assert "Wire the authorization guard" in specs["03-login.md"]
    assert "Component tests" not in (tmp_path / "proj" / ".reincarnate" / "CLAUDE.md").read_text()


def test_no_auth_provider_no_guard(store):
    ids = _seed(store)
    kit.export_kit(store, _comp(store, ids, stack="Python", names=["Board API", "Board view"]),
                   "new:fresh")
    for name, text in _specs(_kd()).items():
        assert "authorization guard" not in text, name


# -- glue steps ---------------------------------------------------------------------------
GLUE_PARTS = [
    ("Board API", "serves board tiles over http", []),
    ("Login", "signs users in with passwords", []),
    ("Tile pinger", "pings each tile service", []),
]


def test_glue_steps_placement_and_numbering(store):
    ids = _seed(store, GLUE_PARTS)
    a, c_ = ids["Board API"], ids["Tile pinger"]
    comp = _comp(
        store, ids, stack="Python", names=["Board API", "Login", "Tile pinger"],
        requirements=[{"id": "r1", "text": "users sign in"},
                      {"id": "r2", "text": "Board tiles show live ping status from each service"},
                      {"id": "r3", "text": "Nightly zipped backups get exported"}],
        coverage=[{"req": "r1", "kind": "catalog", "cap": f"c{ids['Login']}"},
                  {"req": "r2", "kind": "glue", "cap": ""},
                  {"req": "r3", "kind": "glue", "cap": ""}],
        glue=(f"Overview of c{a} and c{c_}.\n\n- Board API tiles poll the ping status from "
              "Tile pinger\n- unrelated cron wiring"),
        gaps=[{"need": "audit log", "build_fresh": True}])
    r = kit.export_kit(store, comp, "new:fresh")
    kd = _kd()
    specs = _specs(kd)
    files = list(specs)
    assert files == ["00-scaffold.md", "01-board-api.md", "02-login.md", "03-tile-pinger.md",
                     "04-glue-board-tiles-show-live-ping-status-from-e.md",
                     "05-fresh-audit-log.md", "06-glue-nightly-zipped-backups-get-exported.md"]
    assert r["steps"] == 7
    r2 = specs[files[4]]
    assert r2.startswith("## Glue: Board tiles show live ping status from each service\n")
    assert _section(r2, "Parts it joins") == "- Board API (step 1)\n- Tile pinger (step 3)"
    assert "- Board API tiles poll the ping status from Tile pinger" in r2
    assert "unrelated cron wiring" not in r2
    assert _section(r2, "Acceptance checks") == (
        "- Satisfies requirement r2: Board tiles show live ping status from each service")
    # r3's glue names no part: it joins nothing and goes last, after the build-fresh step
    r3 = specs[files[6]]
    assert "- Satisfies requirement r3: Nightly zipped backups get exported" in r3
    assert "comes after every part and build-fresh step" in r3
    # tasks.md, BLUEPRINT Steps and spec numbering agree
    tasks = (kd / "tasks.md").read_text().splitlines()
    assert tasks[8:10] == [
        "- [ ] Step 4: Glue: Board tiles show live ping status from each service",
        f"  spec: .reincarnate/specs/{files[4]}"]
    assert tasks[12:14] == [
        "- [ ] Step 6: Glue: Nightly zipped backups get exported",
        f"  spec: .reincarnate/specs/{files[6]}"]
    task_files = [ln.split("specs/")[1] for ln in tasks if "spec: " in ln]
    assert task_files == files
    task_nums = [int(m.group(1)) for ln in tasks if (m := re.match(r"- \[ \] Step (\d+):", ln))]
    assert task_nums == [int(f[:2]) for f in files]
    bp = (kd / "BLUEPRINT.md").read_text()
    steps = bp.split("## Steps\n\n")[1].split("\n\n")[0].splitlines()
    assert [int(s.split(".")[0]) for s in steps] == list(range(7))
    assert steps[4] == "4. Step 4 — Glue: Board tiles show live ping status from each service"
    assert steps[5] == "5. Step 5 — build fresh: audit log"
    assert steps[6] == "6. Step 6 — Glue: Nightly zipped backups get exported"
    assert "Overview of Board API (step 1) and Tile pinger (step 3)." in bp


def test_glue_step_before_auth_gets_note(store):
    parts = [("Board API", "serves board tiles over http", ["http:board-api"]),
             ("Tile pinger", "pings each tile service", []),
             ("Login", "signs users in with passwords", ["auth:user-identity"])]
    ids = _seed(store, parts)
    comp = _comp(store, ids, stack="Python", names=["Board API", "Tile pinger", "Login"],
                 requirements=[{"id": "r2",
                                "text": "Board tiles show ping status for each service"}],
                 coverage=[{"req": "r2", "kind": "glue", "cap": ""}],
                 glue="- Board API tiles show ping status from Tile pinger")
    kit.export_kit(store, comp, "new:fresh")
    specs = _specs(_kd())
    assert list(specs)[3] == "03-glue-board-tiles-show-ping-status-for-each-se.md"
    assert "**Cross-cutting: auth arrives in step 4 (Login).**" in specs[list(specs)[3]]
    assert "Cross-cutting" not in specs["02-tile-pinger.md"]  # no http:/ui: token
    assert "- An anonymous request" in specs["04-login.md"]


def test_glue_joins_named_parts_over_relevance(store):
    ids = _seed(store, GLUE_PARTS)
    pinger = ids["Tile pinger"]
    req = [{"id": "r2", "text": "Board tiles show live ping status from each service"}]
    cov = [{"req": "r2", "kind": "glue", "cap": ""}]
    names = ["Board API", "Login", "Tile pinger"]
    # the matched bullet names Login: it wins over the relevance matches (Board API, Tile pinger)
    comp = _comp(store, ids, stack="Python", names=names, requirements=req, coverage=cov,
                 glue="- LOGIN gates tiles showing ping status\n- unrelated cron wiring")
    kit.export_kit(store, comp, "new:fresh")
    specs = _specs(_kd())
    assert list(specs)[1:4] == ["01-board-api.md", "02-login.md",
                                "03-glue-board-tiles-show-live-ping-status-from-e.md"]
    assert _section(specs[list(specs)[3]], "Parts it joins") == "- Login (step 2)"
    # an unresolved `c<id>` in the matched bullet counts as naming that part
    comp = _comp(store, ids, stack="Python", names=names, requirements=req, coverage=cov,
                 glue=f"- c{pinger} feeds tiles their ping status")
    kit.export_kit(store, comp, "new:fresh")
    specs = _specs(_kd())
    assert list(specs)[4] == "04-glue-board-tiles-show-live-ping-status-from-e.md"
    glue = specs[list(specs)[4]]
    assert _section(glue, "Parts it joins") == "- Tile pinger (step 3)"
    assert "- Tile pinger (step 3) feeds tiles their ping status" in glue


def test_unnamed_glue_goes_last_and_lists_related_parts(store):
    """A glue line that names no part joins nothing: word overlap with long behavior text tied
    the real alerts requirement to unrelated parts, so overlap only lists `related` parts."""
    ids = _seed(store, GLUE_PARTS)
    comp = _comp(store, ids, stack="Python", names=["Board API", "Login", "Tile pinger"],
                 requirements=[{"id": "r2",
                                "text": "Board tiles show live ping status from each service"}],
                 coverage=[{"req": "r2", "kind": "glue", "cap": ""}],
                 glue="- tiles poll the ping status",
                 gaps=[{"need": "audit log", "build_fresh": True}])
    kit.export_kit(store, comp, "new:fresh")
    specs = _specs(_kd())
    files = list(specs)
    assert files[-2:] == ["04-fresh-audit-log.md",
                          "05-glue-board-tiles-show-live-ping-status-from-e.md"]
    glue = specs[files[-1]]
    assert "comes after every part and build-fresh step" in _section(glue, "Parts it joins")
    related = _section(glue, "Possibly related parts (word overlap only, not confirmed)")
    assert "Board API (step 1)" in related and "Tile pinger (step 3)" in related

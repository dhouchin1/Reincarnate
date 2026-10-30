"""Kit v0.9: the idea planner (planner.py) and its place in export_kit."""

import json

import pytest

from reincarnate import claude_runner, kit, kitplan, paths, planner
from reincarnate.models import Composition
from test_kitplan import make_comp

SCOPE = {
    "requirements": ["A user can add a task", "A task can be marked done"],
    "decisions": [
        {"id": "D7", "question": "Who may   see the board?", "why": "privacy",
         "options": ["Anyone", "Signed-in users"], "default": "signed-in USERS"},
        {"id": "D9", "question": "Delete or archive done tasks?", "why": "history",
         "options": ["Delete", "Archive"], "default": "Archive"}]}
PLAN = {
    "contracts_md": "## API style\n\nREST under /api; files live in internal/config/.",
    "steps": [
        {"n": 0, "title": "Scaffold", "spec_md": "# Step 0: Scaffold\n\nGoal: scaffold.\n\n"
         "**Acceptance checks:**\n"
         "- `make test` passes\n- `GET /api/health` returns 200"},
        {"n": 1, "title": "Tasks API", "spec_md": "Goal: tasks in internal/config/.\n\n"
         "**Acceptance checks:**\n- POST /api/tasks returns 201"}]}

ANSWER = {"D1": "Anyone"}


class Fake:
    """Answers the scope call and the plan call by their prompt."""

    def __init__(self, scope=SCOPE, plan=PLAN, **kw):
        self.scope, self.plan, self.kw, self.prompts = scope, plan, kw, []

    def __call__(self, prompt, model):
        self.prompts.append(prompt)
        reply = self.scope if "scoping a software kit" in prompt else self.plan
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return claude_runner.RunResult(text=text, cost_usd=0.5, **self.kw)

    def calls(self, which):
        mark = "scoping a software kit"
        return [p for p in self.prompts if (mark in p) == (which == "scope")]


def comp(store, idea="A tiny task board", **kw):
    c = make_comp(store, **kw)
    c.idea, c.pitch = idea, "pitch"
    return store.save_composition(c)


def kd():
    return paths.data_dir() / "new" / "fresh" / ".reincarnate"


def export(store, c, runner, **kw):
    return kit.export_kit(store, c, "new:fresh", planner="idea", plan_runner=runner, **kw)


# -- validation ------------------------------------------------------------------------------
def test_validate_scope_drops_bad_decisions_and_renumbers():
    raw = {"requirements": ["  one ", "one", 5],
           "decisions": [
               {"id": "D4", "question": "No   default?", "options": ["a", "b"], "default": "c"},
               {"id": "D5", "question": "One option?", "options": ["a"], "default": "a"},
               "junk",
               {"id": "D6", "question": "", "options": ["a", "b"], "default": "a"},
               {"id": "D7", "question": "Fine\n  question?", "why": "w",
                "options": ["Yes", "No", "yes", "Maybe", "x", "y"], "default": "NO"}]}
    data, problems = planner.validate_scope(raw)
    assert data["requirements"] == ["one", "5"]
    assert data["decisions"] == [{"id": "D1", "question": "Fine question?", "why": "w",
                                  "options": ["Yes", "No", "Maybe", "x"], "default": "No",
                                  "steps": []}]
    assert len(problems) == 4 and "D4" in problems[0]


def test_validate_scope_caps_decisions_and_needs_requirements():
    many = [{"question": f"Q{i}?", "options": ["a", "b"], "default": "a"} for i in range(11)]
    data, problems = planner.validate_scope({"requirements": ["r"], "decisions": many})
    assert [d["id"] for d in data["decisions"]] == [f"D{i}" for i in range(1, 9)]
    assert len(problems) == 3
    assert planner.validate_scope({"requirements": [], "decisions": many})[0] is None
    assert planner.validate_scope([])[0] is None


def test_validate_plan_numbering_for_new_and_existing():
    assert planner.validate_plan(PLAN, 0)[0]["steps"][1]["title"] == "Tasks API"
    data, problems = planner.validate_plan(PLAN, 1)
    assert data is None and "step 1 is numbered 0" in problems[0]
    shifted = {**PLAN, "steps": [{**s, "n": s["n"] + 1} for s in PLAN["steps"]]}
    assert planner.validate_plan(shifted, 1)[0] is not None
    assert planner.validate_plan(shifted, 0)[0] is None


def test_validate_plan_needs_contracts_titles_and_specs():
    assert "no contracts_md" in planner.validate_plan({**PLAN, "contracts_md": " "}, 0)[1]
    bad = {**PLAN, "steps": [{"n": 0, "title": "", "spec_md": "x"}]}
    assert "step 0 has no title or spec" in planner.validate_plan(bad, 0)[1]
    assert planner.validate_plan({"steps": []}, 0)[1][0] == "no steps"


def test_choose(monkeypatch):
    monkeypatch.delenv("REINCARNATE_PLANNER")
    assert planner.choose() == "idea" and planner.choose("parts") == "parts"
    monkeypatch.setenv("REINCARNATE_PLANNER", "parts")
    assert planner.choose() == "parts" and planner.choose("idea") == "idea"
    with pytest.raises(ValueError, match="unknown planner"):
        planner.choose("both")
    monkeypatch.setenv("REINCARNATE_PLANNER", "nope")
    with pytest.raises(ValueError):
        planner.choose()


def test_lint_specs_uses_the_library_message():
    out = planner.lint_specs(["Python", "FastAPI"], {1: "Hash it with `bcrypt`.", 2: "plain"})
    assert list(out) == [1] and "Spec mentions bcrypt, which is not in the allowed set" in out[1][0]
    assert "(for example hashlib.scrypt)" in out[1][0]
    assert planner.lint_specs([], {1: "bcrypt"}) == {}


# -- the calls -------------------------------------------------------------------------------
def test_prompts_have_idea_and_stack_but_no_catalog_parts(store):
    c = comp(store)
    fake = Fake()
    export(store, c, fake, dry_run=True)
    assert len(fake.prompts) == 2
    for p in fake.prompts:
        assert "A tiny task board" in p and "Python" in p
        assert "Auth" not in p and "Boards" not in p and "own/repo" not in p
    plan_prompt = fake.calls("plan")[0]
    assert "- A user can add a task" in plan_prompt
    assert "not answered, use the default: Signed-in users" in plan_prompt
    assert "requires-python" in plan_prompt  # the version pin for a new project
    assert ".venv/bin/python" in plan_prompt


def test_exports_write_the_planner_kit(store):
    c = comp(store)
    fake = Fake()
    out = export(store, c, fake)
    assert out["planner"] == "idea" and out["steps"] == 2 and len(fake.prompts) == 2
    assert out["plan"]["status"] == "used" and out["plan2"]["status"] == "used"
    assert out["reconcile"]["status"] == "off" and out["reconcile"]["note"]
    assert out["plan_cost"] == out["plan2_cost"] == 0.5 and out["reconcile_cost"] == 0.0
    assert [d["id"] for d in out["decisions"]] == ["D1", "D2"]
    assert out["decisions"][0]["question"] == "Who may see the board?"
    assert out["decisions"][0]["default"] == "Signed-in users"
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert "Not decided; build with the default: Signed-in users" in contracts
    # the plan's own project path is kept, not turned into [path removed]
    assert "internal/config/" in contracts and "[path removed]" not in contracts
    assert "internal/config/" in (kd() / "specs" / "01-tasks-api.md").read_text()
    # the model's own title line is dropped, so the spec has one
    assert (kd() / "specs" / "00-scaffold.md").read_text().count("# Step 0") == 1
    blueprint = (kd() / "BLUEPRINT.md").read_text()
    assert "the idea planner (claude-opus-5-5)" in blueprint
    assert "- A user can add a task" in blueprint
    assert "Auth (from own/repo): not used by this plan" in blueprint
    assert kitplan.CONTRACTS_LINE in (kd() / "CLAUDE.md").read_text()
    assert "NEW_TARGET=1" in (kd() / "run.sh").read_text()
    tasks = (kd() / "tasks.md").read_text()
    assert "- [ ] Step 0: Scaffold" in tasks and "spec: .reincarnate/specs/01-tasks-api.md" in tasks
    m = json.loads((kd() / "manifest.json").read_text())
    assert m["planner"] == "idea" and m["scope"]["model"] == planner.MODEL
    assert m["scope"]["data"]["requirements"][0] == "A user can add a task"
    assert [d["id"] for d in m["plan"]["data"]["open_decisions"]] == ["D1", "D2"]
    assert m["plan"]["data"]["steps"][0]["n"] == 0 and "specs/00-scaffold.md" in m["files"]


def test_preview_rows_match_the_parts_planner_keys(store):
    c = comp(store)
    out = export(store, c, Fake(), dry_run=True)
    row = out["steps"][0]
    assert set(row) == {"n", "kind", "title", "donor", "deepened", "guard_note", "needs",
                        "checks", "spec_head"}
    assert row["kind"] == "scaffold" and row["checks"] == 2 and out["steps"][1]["kind"] == "plan"
    assert not kd().exists()


def test_both_calls_are_cached(store):
    c = comp(store)
    fake = Fake()
    export(store, c, fake)
    out = export(store, c, fake, force=True)
    assert len(fake.prompts) == 2
    assert out["plan"]["status"] == out["plan2"]["status"] == "cached"
    assert out["plan_cost"] == out["plan2_cost"] == 0.0  # nothing spent, as the parts planner


def test_answering_a_decision_reruns_only_the_plan_call(store):
    c = comp(store)
    fake = Fake()
    export(store, c, fake)
    out = export(store, c, fake, force=True, answers={"D1": "Anyone"})
    assert len(fake.calls("scope")) == 1 and len(fake.calls("plan")) == 2
    assert out["plan"]["status"] == "cached" and out["plan2"]["status"] == "used"
    assert "Decided: Anyone" in fake.calls("plan")[1]
    assert "**D1. Who may see the board?** Decided: Anyone" in (kd() / "CONTRACTS.md").read_text()
    assert store.get_composition("app").proposal["decisions"]["D1"]["answer"] == "Anyone"


def test_decisions_only_makes_just_the_scope_call(store):
    c = comp(store)
    fake = Fake()
    out = export(store, c, fake, decisions_only=True)
    assert len(fake.prompts) == 1 and len(out["decisions"]) == 2 and not kd().exists()
    assert out["decisions"][1]["options"] == ["Delete", "Archive"]
    again = export(store, c, fake, decisions_only=True)
    assert len(fake.prompts) == 1 and again["decisions"] == out["decisions"]


def test_bound_answers_apply_and_a_stale_one_warns(store):
    c = comp(store)
    fake = Fake()
    out = export(store, c, fake, dry_run=True, answers={
        "D1": {"question": "Delete or archive done tasks?", "answer": "delete"},
        "D2": {"question": "A question nobody asks", "answer": "x"}})
    by_id = {d["id"]: d for d in out["decisions"]}
    assert by_id["D2"]["answer"] == "Delete" and by_id["D1"]["answer"] is None
    assert any("D2 was not applied" in w for w in out["warnings"])
    assert any("1 open decision is unanswered" in w for w in out["warnings"])
    assert "decisions" not in store.get_composition("app").proposal  # a dry run saves nothing


def test_answer_decisions_reads_an_idea_manifest(store):
    c = comp(store)
    export(store, c, Fake())
    saved = kit.answer_decisions(store, c, {"D2": "Archive"})
    assert saved["D2"]["source"] == "default"


def test_an_empty_composition_exports_on_the_idea_path_only(store):
    c = Composition(slug="bare", name="Bare", idea="A habit tracker", target_kind="new",
                    target_slug="fresh", proposal={"stack": "Python"})
    c = store.save_composition(c)
    fake = Fake()
    out = export(store, c, fake)
    assert out["steps"] == 2 and "(none)" in (kd() / "BLUEPRINT.md").read_text()
    with pytest.raises(ValueError, match="no capabilities and no gaps"):
        kit.export_kit(store, c, "new:fresh", planner="parts", plan_runner=fake, force=True)


def test_the_library_lint_note_lands_in_the_spec(store):
    plan = {**PLAN, "steps": [PLAN["steps"][0], {**PLAN["steps"][1],
            "spec_md": "Hash passwords with `bcrypt`.\n\n**Acceptance checks:**\n- a"}]}
    out = export(store, comp(store), Fake(plan=plan))
    text = (kd() / "specs" / "01-tasks-api.md").read_text()
    assert "**Contract notes:**" in text and "Spec mentions bcrypt" in text
    assert any(w.startswith("contract lint, step 1:") for w in out["warnings"])


def test_failed_calls_refuse(store):
    c = comp(store)
    with pytest.raises(ValueError, match="idea planner could not plan this kit: .*no JSON"):
        export(store, c, Fake(scope="not json"))
    assert not kd().exists()
    out = export(store, c, Fake(scope="not json"), dry_run=True)
    assert out["refusal"] and "could not plan this kit" in out["refusal"][0]
    with pytest.raises(ValueError, match="no contracts_md"):
        export(store, c, Fake(plan={"steps": PLAN["steps"], "contracts_md": ""}))
    gone = Fake(rc=127)
    with pytest.raises(ValueError, match="claude not found"):
        export(store, c, gone)
    dry = export(store, c, gone, dry_run=True)
    assert dry["plan2"]["note"] == "claude is not available" and dry["refusal"]



def test_claude_missing_on_the_scope_call(store):
    out = export(store, comp(store), Fake(rc=127), dry_run=True)
    assert out["plan"]["note"] == "claude is not available" and out["refusal"]
    assert out["plan2"]["status"] == "off"


def test_ticks_carry_over_and_edits_are_refused_before_any_call(store):
    c = comp(store)
    fake = Fake()
    export(store, c, fake)
    tasks = kd() / "tasks.md"
    tasks.write_text(tasks.read_text().replace("- [ ] Step 0:", "- [x] Step 0:"))
    with pytest.raises(ValueError, match="1 ticked step"):
        export(store, c, fake)
    assert len(fake.prompts) == 2  # refused before any model call (these are the first two)
    out = export(store, c, fake, force=True)
    assert "- [x] Step 0: Scaffold" in tasks.read_text() and "- [ ] Step 1" in tasks.read_text()
    assert any("backed up" in w for w in out["warnings"])
    (kd() / "CONTRACTS.md").write_text("edited")
    with pytest.raises(ValueError, match="edited by hand: CONTRACTS.md"):
        export(store, c, fake)


def test_a_stray_spec_the_plan_would_overwrite_is_refused(store):
    c = comp(store)
    export(store, c, Fake())
    (kd() / "specs" / "02-extra.md").write_text("mine")
    three = {**PLAN, "steps": [*PLAN["steps"], {"n": 2, "title": "Extra",
                                                  "spec_md": "x\n\n**Acceptance checks:**\n- a"}]}
    with pytest.raises(ValueError, match="would overwrite: specs/02-extra.md"):
        export(store, c, Fake(plan=three), answers=ANSWER)  # a new answer: a new plan call
    assert (kd() / "specs" / "02-extra.md").read_text() == "mine"
    out = export(store, c, Fake(plan=three), force=True, answers=ANSWER)
    assert any("backed up" in w for w in out["warnings"])
    assert (kd() / "specs" / "02-extra.md").read_text().startswith("# Step 2: Extra")
    assert list(kd().glob("backup-*/specs/02-extra.md"))


def test_no_plan_falls_back_to_the_parts_planner(store):
    c = comp(store)
    out = kit.export_kit(store, c, "new:fresh", planner="idea", plan=False)
    assert out["planner"] == "parts" and "--no-plan uses the parts planner" in out["warnings"]
    assert json.loads((kd() / "manifest.json").read_text())["planner"] == "parts"


def test_a_bad_planner_value_raises(store):
    with pytest.raises(ValueError, match="unknown planner"):
        kit.export_kit(store, comp(store), "new:fresh", planner="both")


def test_existing_projects_get_no_scaffold(store, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    c = comp(store)
    shifted = {**PLAN, "steps": [{**s, "n": s["n"] + 1} for s in PLAN["steps"]]}
    fake = Fake(plan=shifted)
    out = kit.export_kit(store, c, str(proj), planner="idea", plan_runner=fake)
    assert out["steps"] == 2
    prompt = fake.calls("plan")[0]
    assert "numbered from 1" in prompt and "no scaffold" in prompt.lower()
    assert (proj / ".reincarnate" / "specs" / "01-scaffold.md").is_file()

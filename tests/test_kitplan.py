"""Kit v0.6 slice B: the contracts and scope pass (kitplan.py and its place in export_kit)."""

import json

import pytest

from reincarnate import claude_runner, estimate, kit, kitplan, paths, salvage
from reincarnate.models import Composition, Profile
from reincarnate.prompts import UNTRUSTED_DATA_RULE

GOOD = {
    "api_style": "REST + JSON under /api, errors as {error, message}, one guard "
                 "authorize(request, role)",
    "access_model": ["Anyone signed in may view boards", "Only admins edit boards"],
    "settings": [{"key": "SESSION_SECRET", "step": 1, "meaning": "signs session cookies"}],
    "steps": [
        {"n": 0, "provides": [{"name": "authorize(request, role)", "kind": "module",
                               "consumers": [1, 2]}]},
        {"n": 1, "in_scope": ["local sign-in"], "out_of_scope": ["LDAP and OIDC"],
         "provides": [{"name": "POST /api/auth/login", "kind": "route", "token": "auth:session",
                       "consumers": [2]}],
         "uses": [{"name": "authorize(request, role)", "from": 0}],
         "library_note": "Use the stack's own password hashing."},
        {"n": 2, "in_scope": ["list boards"],
         "uses": [{"name": "POST /api/auth/login", "from": 1}], "checks": ["not for caps"]},
        {"n": 3, "in_scope": ["alert when a board is down"],
         "checks": ["An alert shows when a board is down", "The alert clears when it is up"]},
    ],
}


def seed(store):
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", {
        "summary": "A tool.", "dependencies": {"license": "MIT"},
        "capabilities": [
            {"name": "Auth", "what": "Local sign-in; also LDAP and OIDC. See src/secret_auth.py.",
             "portability": "high", "files": ["src/secret_auth.py"], "provides": ["auth:session"]},
            {"name": "Boards", "what": "Lists boards.", "portability": "high",
             "files": ["src/boards.py"], "requires": ["auth:session"]}]})
    return {c.name: c.id for c in store.list_capabilities()}


def make_comp(store, target_kind="new", target_slug="fresh", slug="app"):
    ids = seed(store) if store.list_capabilities() == [] else {
        c.name: c.id for c in store.list_capabilities()}
    a, b = ids["Auth"], ids["Boards"]
    proposal = {
        "capabilities": [{"capability_id": a, "order": 1, "name": "Auth", "donor": "own/repo"},
                         {"capability_id": b, "order": 2, "name": "Boards", "donor": "own/repo"}],
        "stack": "Python", "gaps": [],
        "requirements": [{"id": "r1", "text": "Local sign-in"},
                         {"id": "r2", "text": "Alert when a board is down"}],
        "coverage": [{"req": "r1", "kind": "catalog", "cap": f"c{a}"},
                     {"req": "r2", "kind": "glue", "cap": ""}],
        "glue": "- Boards alert when a board is down",
        "wiring": {"wired": [{"token": "auth:session", "consumer": {"id": b, "name": "Boards"},
                              "provider": {"id": a, "name": "Auth"}}]}}
    return store.save_composition(Composition(
        slug=slug, name="App", target_kind=target_kind, target_slug=target_slug,
        proposal=proposal))


class Runner:
    def __init__(self, reply=None, **kw):
        self.reply, self.kw, self.prompts = reply, kw, []

    def __call__(self, prompt, model):
        self.prompts.append(prompt)
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return claude_runner.RunResult(text=text, cost_usd=0.05, **self.kw)


def warns(out):
    return [w for w in out["warnings"] if "not deepened" not in w]


def kd():
    return paths.data_dir() / "new" / "fresh" / ".reincarnate"


def spec(prefix):
    return next((kd() / "specs").glob(f"{prefix}-*.md")).read_text()


def test_good_plan_writes_contracts_and_specs(store):
    comp = make_comp(store)
    runner = Runner(GOOD)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    assert warns(out) == [] and out["plan"]["status"] == "used"
    assert out["plan_cost"] == 0.05 and len(runner.prompts) == 1
    assert "CONTRACTS.md" in out["files"]
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert "## API style" in contracts and "REST + JSON under /api" in contracts
    assert "Only admins edit boards" in contracts and "`SESSION_SECRET`" in contracts
    assert "`POST /api/auth/login` | route | step 2 (Boards)" in contracts
    assert "`authorize(request, role)` | step 0 (scaffold)" in contracts
    claude = (kd() / "CLAUDE.md").read_text()
    assert kitplan.CONTRACTS_LINE in claude
    auth = spec("01")
    assert "**Scope:**" in auth and "- local sign-in" in auth and "- LDAP and OIDC" in auth
    assert "**Provides to later steps**" in auth and "`POST /api/auth/login` (route)" in auth
    assert "**Uses from earlier steps:**" in auth and "**Library note:**" in auth
    boards = spec("02")
    assert "**Uses from earlier steps:**" in boards
    assert "Needs from earlier steps" not in boards  # replaced by the plan's uses
    assert "not for caps" not in boards  # checks only for glue and build-fresh steps
    glue = spec("03")
    assert "**Acceptance checks (from the requirement):**" in glue
    assert "- The alert clears when it is up" in glue
    assert "**Provides to later steps" in spec("00")  # the scaffold carries the guard skeleton
    manifest = json.loads((kd() / "manifest.json").read_text())
    assert manifest["plan"]["cost_usd"] == 0.05 and manifest["plan"]["model"] == kitplan.MODEL
    assert manifest["plan"]["data"]["steps"][1]["n"] == 1
    assert "CONTRACTS.md" in manifest["files"]


def test_prompt_is_catalog_text_only_with_the_untrusted_rule(store):
    comp = make_comp(store)
    runner = Runner(GOOD)
    kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    prompt = runner.prompts[0]
    assert UNTRUSTED_DATA_RULE in prompt and prompt.index(UNTRUSTED_DATA_RULE) < prompt.index("{")
    assert "secret_auth" not in prompt and "boards.py" not in prompt
    assert "auth:session" in prompt and "LDAP and OIDC" in prompt


def test_reexport_reuses_the_cached_plan_and_a_change_makes_a_new_call(store):
    comp = make_comp(store)
    runner = Runner(GOOD)
    kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    assert len(runner.prompts) == 1 and out["plan"]["status"] == "cached"
    assert out["plan_cost"] == 0.0 and "**Scope:**" in spec("01")
    assert json.loads((kd() / "manifest.json").read_text())["plan"]["cost_usd"] == 0.05
    comp.proposal["requirements"][0]["text"] = "Local sign-in with a password"
    store.save_composition(comp)
    kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    assert len(runner.prompts) == 2


@pytest.mark.parametrize("runner", [
    Runner(GOOD, rc=1),
    Runner("sorry, no JSON here"),
    Runner({"api_style": "x", "steps": []}),
    Runner({"steps": [{"n": 99, "in_scope": ["x"]}]}),
])
def test_a_failed_pass_falls_back_with_a_warning(store, runner):
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    assert any(w.startswith("contracts pass skipped: ") for w in out["warnings"])
    assert out["plan"]["status"] == "skipped"
    assert "CONTRACTS.md" not in out["files"] and not (kd() / "CONTRACTS.md").exists()
    assert "**Scope:**" not in spec("01") and "Needs from earlier steps" in spec("02")
    assert kitplan.CONTRACTS_LINE not in (kd() / "CLAUDE.md").read_text()
    assert "plan" not in json.loads((kd() / "manifest.json").read_text())


def test_a_raising_runner_never_fails_the_export(store):
    comp = make_comp(store)

    def boom(prompt, model):
        raise RuntimeError("kaboom")

    out = kit.export_kit(store, comp, "new:fresh", plan_runner=boom)
    assert any("contracts pass skipped" in w and "kaboom" in w for w in out["warnings"])
    assert out["steps"] == 4


def test_missing_claude_skips_quietly(store):
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh")  # conftest: no real claude
    assert warns(out) == [] and out["plan"]["status"] == "skipped"
    assert "claude is not available" in out["plan"]["note"]
    assert (kd() / "specs").is_dir()


def test_no_plan_makes_no_call_and_only_a_neutral_note(store):
    comp = make_comp(store)
    runner = Runner(GOOD)
    out = kit.export_kit(store, comp, "new:fresh", plan=False, plan_runner=runner)
    assert runner.prompts == [] and warns(out) == []
    assert out["plan"]["status"] == "off" and "--no-plan" in out["plan"]["note"]
    assert "CONTRACTS.md" not in out["files"]


def test_turning_the_pass_off_removes_a_stale_contracts_file(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan_runner=Runner(GOOD))
    assert (kd() / "CONTRACTS.md").is_file()
    kit.export_kit(store, comp, "new:fresh", plan=False)
    assert not (kd() / "CONTRACTS.md").exists()


def test_refusal_comes_before_any_model_call(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan=False)
    tasks = kd() / "tasks.md"
    tasks.write_text(tasks.read_text().replace("- [ ] Step 1", "- [x] Step 1"))
    runner = Runner(GOOD)
    with pytest.raises(ValueError, match="progress or edits"):
        kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    assert runner.prompts == []
    out = kit.export_kit(store, comp, "new:fresh", dry_run=True, plan_runner=runner)
    assert out["refusal"] and runner.prompts == [] and out["plan"]["status"] == "skipped"


def test_hand_edited_contracts_block_a_reexport(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan_runner=Runner(GOOD))
    (kd() / "CONTRACTS.md").write_text("mine\n")
    with pytest.raises(ValueError, match="edited by hand: CONTRACTS.md"):
        kit.export_kit(store, comp, "new:fresh", plan_runner=Runner(GOOD))


def test_dry_run_runs_the_pass_and_writes_nothing(store):
    comp = make_comp(store)
    runner = Runner(GOOD)
    out = kit.export_kit(store, comp, "new:fresh", dry_run=True, plan_runner=runner)
    assert len(runner.prompts) == 1 and not kd().exists()
    assert "CONTRACTS.md" in out["files"] and out["plan"]["status"] == "used"
    heads = {s["n"]: s["spec_head"] for s in out["steps"]}
    assert "**Scope:**" in heads[1] and "**Scope:**" in heads[3]
    assert "## Step 1 (Auth)" in out["files"]["CONTRACTS.md"]


def test_zip_lists_contracts(store):
    import zipfile
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", zip_it=True, plan_runner=Runner(GOOD))
    with zipfile.ZipFile(out["zip"]) as z:
        assert any(n.endswith("CONTRACTS.md") for n in z.namelist())


def test_existing_target_has_no_step_zero(store, tmp_path):
    comp = make_comp(store, target_kind="existing", target_slug="proj")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='p'\n")
    runner = Runner(GOOD)
    out = kit.export_kit(store, comp, str(proj), plan_runner=runner)
    assert out["steps"] == 3  # steps 1-3; the plan's step 0 and its uses of it are dropped
    assert any("unknown step 0" in w for w in out["warnings"])
    assert any("uses 'authorize(request, role)'" in w for w in out["warnings"])
    assert (proj / ".reincarnate" / "CONTRACTS.md").is_file()


def test_a_kit_without_wiring_still_plans(store):
    comp = make_comp(store)
    comp.proposal["wiring"] = {}
    store.save_composition(comp)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=Runner(GOOD))
    assert out["plan"]["status"] == "used"


# -- validate -------------------------------------------------------------------------------
def _steps():
    return [{"n": 0, "kind": "scaffold"}, {"n": 1, "kind": "cap"}, {"n": 2, "kind": "cap"},
            {"n": 3, "kind": "glue"}]


def test_validate_drops_bad_uses_and_reports_uncovered_tokens():
    raw = {"steps": [
        {"n": 1, "provides": [{"name": "POST /api/auth/login", "kind": "route",
                               "consumers": [2, 9, 1]}],
         "uses": [{"name": "later thing", "from": 2}]},
        {"n": 2, "provides": [{"name": "GET /api/boards", "kind": "weird"}],
         "uses": [{"name": "POST /api/auth/login", "from": 1}, {"name": "ghost", "from": 1},
                  {"name": "x", "from": "no"}]},
        {"n": 7, "provides": [{"name": "x"}]}]}
    wiring = [{"token": "auth:session", "provider": 1, "consumer": 2},
              {"token": "data:boards", "provider": 2, "consumer": 3}]
    plan, problems = kitplan.validate(raw, _steps(), wiring)
    by = {e["n"]: e for e in plan["steps"]}
    assert sorted(by) == [1, 2]
    assert by[1]["uses"] == [] and by[1]["provides"][0]["consumers"] == [2]
    assert by[2]["uses"] == [{"name": "POST /api/auth/login", "from": 1}]
    assert by[2]["provides"][0]["kind"] == "module"
    text = "\n".join(problems)
    assert "later or unknown step" in text and "'ghost', which step 1 does not provide" in text
    assert "unknown step 7" in text
    assert "wired token auth:session from step 1 is not covered" in text
    assert "data:boards from step 2" in text


def test_validate_covers_a_token_by_its_field_or_its_name():
    raw = {"steps": [{"n": 1, "provides": [{"name": "session cookie", "token": "auth:session"}]},
                     {"n": 2, "provides": [{"name": "the data:boards table", "kind": "record"}]}]}
    wiring = [{"token": "auth:session", "provider": 1, "consumer": 2},
              {"token": "data:boards", "provider": 2, "consumer": 3}]
    assert kitplan.validate(raw, _steps(), wiring)[1] == []


def test_validate_gates_strings_and_keeps_checks_to_glue_and_fresh():
    raw = {"api_style": "REST; see `src/app/main.py` for the shape",
           "access_model": ["admins use /Users/dan/secret/notes.md"],
           "steps": [{"n": 3, "in_scope": ["read src/donor/thing.py first"],
                      "checks": ["a", "b", "c", "d", "e"]},
                     {"n": 1, "checks": ["nope"], "library_note": "use lib in src/x/y.py"}]}
    plan, _ = kitplan.validate(raw, _steps(), [])
    blob = json.dumps(plan)
    assert "src/app/main.py" not in blob and "src/donor" not in blob
    assert "/Users/dan" not in blob and "src/x/y.py" not in blob
    assert salvage.DONOR_FILE in blob
    by = {e["n"]: e for e in plan["steps"]}
    assert len(by[3]["checks"]) == kitplan.MAX_CHECKS and by[1]["checks"] == []


def test_validate_rejects_unusable_replies():
    assert kitplan.validate([], _steps(), [])[0] is None
    plan, problems = kitplan.validate({"steps": "no"}, _steps(), [])
    assert plan is None and "no usable steps" in problems[-1]


def test_build_input_clips_and_carries_definitions(store):
    comp = make_comp(store)
    cap = store.get_capability(comp.proposal["capabilities"][0]["capability_id"])
    cap.what = ("word " * 400).strip()
    st = [{"n": 1, "kind": "cap", "cap": cap, "role": "sign-in"}]
    data = kitplan.build_input(store, st, comp.proposal, {"kind": "new", "stack": ["python"]})
    what = data["steps"][0]["what"]
    assert len(what) <= kitplan.WHAT_CAP + 3 and what.endswith("...")
    assert data["steps"][0]["provides"][0]["token"] == "auth:session"
    assert data["target"]["stack"] == ["python"]
    assert kitplan.cache_key(data, "m1") != kitplan.cache_key(data, "m2")


def test_default_runner_is_one_isolated_toolless_call(store, monkeypatch):
    calls = []

    def fake_run(prompt, **kw):
        calls.append(kw)
        return claude_runner.RunResult(text=json.dumps(GOOD), cost_usd=0.04)

    monkeypatch.setattr(claude_runner, "run", fake_run)
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh")
    # the export also runs the reconcile pass (v0.7); every model call is isolated and tool-less,
    # and exactly one of them is the contracts pass
    plan_calls = [c for c in calls if "reincarnate-plan-" in str(c.get("cwd"))]
    assert len(plan_calls) == 1
    assert all(c["tools"] == "" and c["max_turns"] == 1 for c in calls)
    assert out["plan_cost"] == 0.04


def test_estimate_plan_grows_with_steps():
    a, b = estimate.estimate_plan(2), estimate.estimate_plan(8)
    assert a["low"] < a["high"] and b["high"] > a["high"]
    assert a["low"] == round((0.03 + 0.006 * 2) * 0.5, 4)


def test_placeholder_warnings_from_the_spec_lint_join_the_kit_warnings(store, monkeypatch):
    comp = make_comp(store)
    monkeypatch.setattr(salvage, "placeholder_warnings",
                        lambda cap: ["check: ' 0 '"] if cap.name == "Auth" else [])
    out = kit.export_kit(store, comp, "new:fresh", plan=False)
    assert "Auth: looks like a placeholder, check it: check: ' 0 '" in out["warnings"]


def test_a_preview_and_a_later_export_share_one_paid_pass(store):
    """The dry-run preview writes no kit, so the plan is cached in the data folder too."""
    comp = make_comp(store)
    runner = Runner(GOOD)
    kit.export_kit(store, comp, "new:fresh", dry_run=True, plan_runner=runner)
    kit.export_kit(store, comp, "new:fresh", dry_run=True, plan_runner=runner)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=runner)
    assert len(runner.prompts) == 1 and out["plan"]["status"] == "cached"
    assert "**Scope:**" in spec("01")


def test_uses_match_the_providers_own_wording():
    provides = [{"name": "current_user / require_user / require_admin guards"},
                {"name": "error response handler {error:{code,message}}"},
                {"name": "users table"}, {"name": "users route"}]
    assert (kitplan._provided_as("require_admin guard", provides)
            == "current_user / require_user / require_admin guards")
    assert (kitplan._provided_as("error response handler", provides)
            == "error response handler {error:{code,message}}")
    assert kitplan._provided_as("users", provides) is None  # ambiguous: two entries match
    assert kitplan._provided_as("session store", provides) is None


# -- open decisions ---------------------------------------------------------------------------
ASKED = {**GOOD, "open_decisions": [
    {"id": "D1", "question": "Can anonymous visitors see the board?",
     "why": "decides whether the board routes need a guard",
     "options": ["Only signed-in users", "Anyone with the link"], "default": "Only signed-in users",
     "steps": [1, 2]},
    {"id": "D2", "question": "May editors delete boards?", "why": "roles",
     "options": ["Yes", "No"], "default": "No", "steps": [2]}]}


def _asked_runner(question=None):
    reply = json.loads(json.dumps(ASKED))
    if question:
        reply["open_decisions"][0]["question"] = question
    return Runner(reply)


def test_open_decisions_render_with_defaults_and_ride_in_the_result(store):
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner())
    contracts = (kd() / "CONTRACTS.md").read_text()
    head, _, rest = contracts.partition("## Decisions")
    assert "## Who may do what" in head and "## Settings" in rest.split("## Step")[0]
    assert ("**D1. Can anonymous visitors see the board?** Not decided; build with the "
            "default: Only signed-in users. (Ask the user before changing it.)") in rest
    for n in ("01", "02"):
        assert "**Decisions that apply:**" in spec(n) and "**D1. Can anonymous" in spec(n)
    assert "**D2." in spec("02") and "**D2." not in spec("01")
    assert "Decisions that apply" not in spec("03") and "Decisions that apply" not in spec("00")
    assert "open decisions" in (kd() / "CLAUDE.md").read_text()
    d1 = out["decisions"][0]
    assert d1 == {"id": "D1", "question": "Can anonymous visitors see the board?",
                  "why": "decides whether the board routes need a guard",
                  "options": ["Only signed-in users", "Anyone with the link"],
                  "default": "Only signed-in users", "steps": [1, 2], "answer": None,
                  "source": None}
    msg = [w for w in out["warnings"] if "unanswered" in w]
    assert len(msg) == 1 and msg[0].startswith("2 open decisions are unanswered; the builder")
    assert "--answers FILE" in msg[0]


def test_answers_are_saved_and_the_next_export_uses_the_cache(store):
    comp = make_comp(store)
    runner = _asked_runner()
    kit.export_kit(store, comp, "new:fresh", dry_run=True, plan_runner=runner)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=runner,
                         answers={"D1": "anyone with the link",
                                  "D2": "Only on weekends. See /etc/x.py"})
    # the asking pass (phase 1) was cached; the one new call is phase 2, which re-plans the
    # contracts with the answers (v0.8)
    assert len(runner.prompts) == 2 and "These decisions are final" in runner.prompts[1]
    saved = store.get_composition("app").proposal["decisions"]
    assert saved["D1"]["answer"] == "Anyone with the link" and saved["D1"]["source"] == "user"
    assert saved["D2"]["source"] == "user" and "/etc/x.py" not in saved["D2"]["answer"]
    assert saved["D1"]["question"] == "Can anonymous visitors see the board?"
    assert not [w for w in out["warnings"] if "unanswered" in w]
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert ("**D1. Can anonymous visitors see the board?** Decided: Anyone with the link"
            in contracts)
    assert "Not decided" not in contracts and "Decided: Anyone" in spec("02")
    again = kit.export_kit(store, store.get_composition("app"), "new:fresh", plan_runner=runner)
    assert len(runner.prompts) == 2 and again["decisions"][1]["answer"]  # both phases cached


def test_a_default_answer_is_recorded_as_such(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner(), answers={"D2": "No"})
    assert store.get_composition("app").proposal["decisions"]["D2"]["source"] == "default"


def test_bad_answers_save_nothing(store):
    comp = make_comp(store)
    with pytest.raises(ValueError, match=r"unknown decision id\(s\) D9.*D1, D2"):
        kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner(),
                       answers={"D1": "Yes", "D9": "x"})
    assert "decisions" not in store.get_composition("app").proposal
    assert not kd().exists()
    with pytest.raises(ValueError, match="non-empty text"):
        kit.answer_decisions(store, comp, {"D1": "  "}, decisions=ASKED["open_decisions"])
    with pytest.raises(ValueError, match="over 2000"):
        kit.answer_decisions(store, comp, {"D1": "x" * 2001}, decisions=ASKED["open_decisions"])
    with pytest.raises(ValueError, match="no plan"):
        kit.answer_decisions(store, comp, {"D1": "x"})


def test_a_bound_answer_follows_its_question_not_its_id(store):
    # the answers were given against a plan that numbered the questions the other way round
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner(), answers={
        "D1": {"question": "May editors delete boards?", "answer": "Yes"},
        "D2": {"question": "can anonymous visitors see the BOARD",
               "answer": "Anyone with the link"}})
    saved = store.get_composition("app").proposal["decisions"]
    assert saved["D1"]["answer"] == "Anyone with the link" and saved["D2"]["answer"] == "Yes"
    assert [d["answer"] for d in out["decisions"]] == ["Anyone with the link", "Yes"]
    assert not any("not applied" in w for w in out["warnings"])


def test_an_answer_to_a_question_no_longer_asked_is_not_applied(store):
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner(), answers={
        "D1": {"question": "How are lyrics stored?", "answer": "Anyone with the link"},
        "D2": {"question": "May editors delete boards?", "answer": "Yes"}})
    saved = store.get_composition("app").proposal["decisions"]
    assert "D1" not in saved and saved["D2"]["answer"] == "Yes"
    assert out["decisions"][0]["answer"] is None
    assert any("D1 was not applied" in w and "How are lyrics stored?" in w
               for w in out["warnings"])
    assert "Decided: Anyone" not in (kd() / "CONTRACTS.md").read_text()
    with pytest.raises(ValueError, match="no question text"):
        kit.answer_decisions(store, comp, {"D1": {"answer": "x"}},
                             decisions=ASKED["open_decisions"])


def test_decisions_only_deepens_first_when_asked(store, monkeypatch):
    comp = make_comp(store)
    called = []
    monkeypatch.setattr(kit, "_run_deepen", lambda st, ids: called.append(ids) or {
        "capabilities": ids, "deepened": ids, "skipped": [], "failed": [], "cost_usd": 0.04})
    out = kit.export_kit(store, comp, "new:fresh", decisions_only=True, deepen=True,
                         plan_runner=_asked_runner())
    assert called and len(out["decisions"]) == 2 and not kd().exists()
    called.clear()
    kit.export_kit(store, comp, "new:fresh", decisions_only=True, plan_runner=_asked_runner())
    assert not called


def test_answer_decisions_reads_the_kit_manifest_without_a_plan_argument(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner())
    saved = kit.answer_decisions(store, comp, {"D1": "Only signed-in users"})
    assert saved["D1"]["source"] == "default"
    assert store.get_composition("app").proposal["decisions"]["D1"]["answer"]


def test_dry_run_with_answers_saves_nothing(store):
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", dry_run=True, plan_runner=_asked_runner(),
                         answers={"D1": "Anyone with the link"})
    assert out["decisions"][0]["answer"] == "Anyone with the link"
    assert "decisions" not in store.get_composition("app").proposal


def test_a_reworded_question_is_asked_again_and_an_unchanged_one_keeps_its_answer(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan_runner=_asked_runner(),
                   answers={"D1": "Anyone with the link", "D2": "Yes"})
    comp = store.get_composition("app")
    comp.proposal["requirements"][0]["text"] = "Local sign-in, no SSO"  # a new input: a new call
    store.save_composition(comp)
    runner = _asked_runner(question="Are board pages public?")
    out = kit.export_kit(store, comp, "new:fresh", force=True, plan_runner=runner)
    assert len(runner.prompts) == 2  # phase 1 for the new input, phase 2 for the kept answer
    by_id = {d["id"]: d for d in out["decisions"]}
    assert by_id["D1"]["answer"] is None and by_id["D2"]["answer"] == "Yes"
    assert any("1 open decision is unanswered" in w for w in out["warnings"])
    assert "Not decided; build with the default: Only signed-in users" in (
        kd() / "CONTRACTS.md").read_text()
    assert store.get_composition("app").proposal["decisions"]["D1"]["answer"] == (
        "Anyone with the link")  # kept on the composition; it just does not apply now


def test_validate_drops_bad_open_decisions_and_caps_them():
    ok = {"id": "D1", "question": "q?", "options": ["a", "b"], "default": "A", "steps": [1]}
    raw = {**GOOD, "open_decisions": [
        ok, {**ok},  # duplicate id
        {**ok, "id": "D2", "default": "c"},  # default not among the options
        {**ok, "id": "D3", "steps": [9]},  # step out of range
        {**ok, "id": "D4", "options": ["only"], "default": "only"},
        {**ok, "id": "x", "question": "bad id"}, {**ok, "id": "D5", "question": " "},
        {**ok, "id": "D6", "steps": []}, "junk"]}
    pl, problems = kitplan.validate(raw, _steps(), [])
    assert [d["id"] for d in pl["open_decisions"]] == ["D1"]
    assert pl["open_decisions"][0]["default"] == "a"  # the option's own spelling
    assert sum("open decision" in p for p in problems) == 7
    many = [{**ok, "id": f"D{i}"} for i in range(1, 10)]
    pl, problems = kitplan.validate({**GOOD, "open_decisions": many}, _steps(), [])
    assert len(pl["open_decisions"]) == 6 and any("more than 6" in p for p in problems)
    assert kitplan.validate(GOOD, _steps(), [])[0]["open_decisions"] == []


def test_the_prompt_asks_for_decisions():
    assert "open_decisions" in kitplan.SCHEMA_HINT
    assert json.loads(kitplan.SCHEMA_HINT)["open_decisions"][0]["id"] == "D1"


def test_no_plan_means_no_decisions_and_no_warning_about_them(store):
    comp = make_comp(store)
    out = kit.export_kit(store, comp, "new:fresh", plan=False)
    assert out["decisions"] == [] and not [w for w in out["warnings"] if "decision" in w]
    out = kit.export_kit(store, comp, "new:fresh", force=True,
                         plan_runner=Runner("not json at all"))
    assert out["decisions"] == [] and not [w for w in out["warnings"] if "unanswered" in w]
    with pytest.raises(ValueError, match="no-plan"):
        kit.export_kit(store, comp, "new:fresh", force=True, plan=False, answers={"D1": "x"})


def test_cli_answers_and_decisions_only(store, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace as NS
    comp = make_comp(store)
    runner = _asked_runner()
    monkeypatch.setattr(kitplan, "_default_runner", runner)
    monkeypatch.setenv("REINCARNATE_QUIET", "1")
    asked = kit.run_cli(store, NS(composition=comp.slug, into="new:fresh", decisions=True))
    assert list(asked) == ["decisions"] and len(asked["decisions"]) == 2
    assert not kd().exists() and len(runner.prompts) == 1
    f = tmp_path / "answers.json"
    f.write_text(json.dumps({"D1": "Anyone with the link"}))
    out = kit.run_cli(store, NS(composition=comp.slug, into="new:fresh", answers=str(f)))
    # the cache served phase 1; the one new call is phase 2 with the answer
    assert "error" not in out and len(runner.prompts) == 2
    assert "Decided: Anyone with the link" in (kd() / "CONTRACTS.md").read_text()
    f.write_text("[1]")
    assert "must hold a JSON object" in kit.run_cli(
        store, NS(composition=comp.slug, into="new:fresh", answers=str(f)))["error"]
    f.write_text("{")
    assert "cannot read --answers" in kit.run_cli(
        store, NS(composition=comp.slug, into="new:fresh", answers=str(f)))["error"]
    f.write_text('{"D7": "x"}')
    assert "unknown decision id" in kit.run_cli(
        store, NS(composition=comp.slug, into="new:fresh", answers=str(f), force=True))["error"]


def test_decisions_only_asks_even_when_the_kit_has_progress(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan=False)
    tasks = kd() / "tasks.md"
    tasks.write_text(tasks.read_text().replace("- [ ] Step 1", "- [x] Step 1"))
    runner = _asked_runner()
    out = kit.export_kit(store, comp, "new:fresh", decisions_only=True, plan_runner=runner)
    assert len(out["decisions"]) == 2 and "- [x] Step 1" in tasks.read_text()


# -- v0.7 slice A: fields, gate, consumers, conflicts ---------------------------------------
FIELDED = json.loads(json.dumps(GOOD))
FIELDED["steps"][0]["provides"][0]["fields"] = {
    "signature": "authorize(request, role) -> user\nrequire_admin(request) -> user"}
FIELDED["steps"][1]["provides"][0]["fields"] = {
    "request": [{"name": "username", "type": "string", "meaning": "the login"},
                {"name": "password", "type": "string"}],
    "response": [{"name": "user", "type": "object", "meaning": "the signed-in user"}],
    "errors": [401, "429"]}
FIELDED["steps"][1]["provides"].append(
    {"name": "boards table", "kind": "record", "consumers": [2],
     "fields": {"fields": [{"name": "id", "type": "int"}, "title"]}})
FIELDED["steps"][1]["provides"].append(
    {"name": "SESSION_DAYS", "kind": "setting", "consumers": [2],
     "fields": {"type": "int", "default": 7}})


def test_fields_reach_contracts_specs_and_uses(store):
    comp = make_comp(store)
    kit.export_kit(store, comp, "new:fresh", plan_runner=Runner(FIELDED))
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert "Request: `username` (string): the login; `password` (string)" in contracts
    assert "Response: `user` (object): the signed-in user" in contracts
    assert "Errors: 401, 429" in contracts
    assert "Signature: `authorize(request, role) -> user`" in contracts
    assert "Signature: `require_admin(request) -> user`" in contracts
    assert "Fields: `id` (int); `title`" in contracts and "Type: int; Default: 7" in contracts
    auth = spec("01")
    assert "  - Request: `username` (string): the login" in auth  # under Provides
    assert "  - Signature: `authorize(request, role) -> user`" in auth  # under Uses, from step 0
    boards = spec("02")  # uses the login route and the table: sees their exact shape
    assert "  - Response: `user` (object): the signed-in user" in boards
    assert "  - Fields: `id` (int); `title`" in boards


def test_validate_keeps_good_fields_and_drops_malformed_parts():
    raw = {"steps": [{"n": 1, "provides": [
        {"name": "POST /a", "kind": "route",
         "fields": {"request": "oops", "response": [{"name": "x" * 500}, 5, None, {"type": "t"}],
                    "errors": "nope"}},
        {"name": "rec", "kind": "record",
         "fields": {"fields": [{"name": f"f{i}"} for i in range(40)]}},
        {"name": "mod", "kind": "module", "fields": ["not", "a", "dict"]},
        {"name": "mod2", "kind": "module", "fields": {"signature": [f"f{i}() -> x"
                                                                    for i in range(9)]}}]}]}
    plan, _ = kitplan.validate(raw, _steps(), [])
    by = {p["name"]: p for p in plan["steps"][0]["provides"]}
    assert len(by) == 4  # no entry is dropped for a bad part
    assert "request" not in by["POST /a"]["fields"] and "errors" not in by["POST /a"]["fields"]
    assert [f["name"][:3] for f in by["POST /a"]["fields"]["response"]] == ["xxx", "5"]
    assert len(by["rec"]["fields"]["fields"]) == kitplan.MAX_FIELDS
    assert by["mod"]["fields"] == {}
    assert len(by["mod2"]["fields"]["signature"].splitlines()) == kitplan.MAX_SIGNATURES


def test_plan_text_keeps_guard_names_and_json_shapes_but_not_donor_paths():
    api = ("REST under /api; guards `current_user` and `require_admin(request)`; errors "
           "`{\"error\": {\"code\": \"x\", \"message\": \"y\"}}`; see `src/app/main.py`, "
           "src/donor/thing.py and https://github.com/own/repo/blob/main/src/a.py")
    raw = {"api_style": api, "access_model": ["only `require_admin(request)` edits"],
           "steps": [{"n": 1, "in_scope": ["call `current_user` first"]}]}
    plan, _ = kitplan.validate(raw, _steps(), [])
    style = plan["api_style"]
    assert "`current_user`" in style and "`require_admin(request)`" in style
    assert '{"error": {"code": "x", "message": "y"}}' in style
    assert "[code removed]" not in style
    for gone in ("src/app/main.py", "src/donor", "github.com"):
        assert gone not in style
    assert "require_admin(request)" in plan["access_model"][0]
    assert plan["steps"][0]["in_scope"] == ["call current_user first"]


def test_a_long_api_style_is_never_cut_mid_word():
    words = " ".join(f"convention{i}" for i in range(300))
    plan, _ = kitplan.validate({"api_style": words, "steps": [{"n": 1}]}, _steps(), [])
    style = plan["api_style"]
    assert len(style) <= kitplan.API_STYLE_CAP and style.endswith("…")
    assert style[:-1].split()[-1] in words.split()
    short = "x " * 100
    assert kitplan.validate({"api_style": short, "steps": [{"n": 1}]}, _steps(),
                            [])[0]["api_style"] == short.strip()


def test_consumers_must_be_later_and_a_consumer_gets_its_uses():
    raw = {"steps": [
        {"n": 1, "provides": [{"name": "GET /api/a", "kind": "route", "consumers": [0, 1, 2, 3]},
                              {"name": "b", "consumers": [2]}]},
        {"n": 2, "uses": [{"name": "b", "from": 1}], "provides": [
            {"name": "c", "consumers": [1]}]},
        {"n": 3}]}
    plan, problems = kitplan.validate(raw, _steps(), [])
    by = {e["n"]: e for e in plan["steps"]}
    assert by[1]["provides"][0]["consumers"] == [2, 3]
    assert by[2]["provides"][0]["consumers"] == []  # step 1 is earlier
    text = "\n".join(problems)
    assert "provides 'GET /api/a' to step 0" in text and "to step 1, which is not later" in text
    assert "provides 'c' to step 1" in text
    assert by[2]["uses"] == [{"name": "b", "from": 1}, {"name": "GET /api/a", "from": 1}]
    assert by[3]["uses"] == [{"name": "GET /api/a", "from": 1}]  # added, none was listed
    assert sum(u["name"] == "b" for u in by[2]["uses"]) == 1  # no duplicate of a listed use


def test_prompt_rev_moved_off_the_v06_value():
    assert kitplan.PROMPT_REV != "decisions-1"
    data = {"steps": []}
    assert kitplan.cache_key(data, "m") != kitplan.hashlib.sha256(
        (json.dumps(data, sort_keys=True) + "\nm\ndecisions-1").encode()).hexdigest()
    prompt = kitplan.build_prompt({"steps": []})
    assert "signature" in prompt and "LATER steps only" in prompt
    assert "Conflicts are decisions" in prompt
    assert "fields" in json.loads(kitplan.SCHEMA_HINT)["steps"][0]["provides"][0]


def _tls_inputs(store):
    comp = make_comp(store)
    cap = store.get_capability(comp.proposal["capabilities"][1]["capability_id"])
    cap.security_notes = "The donor intentionally has TLS verification disabled for probes."
    cap.key_values = [{"name": "verify_tls", "value": "false"}]
    proposal = json.loads(json.dumps(comp.proposal))
    proposal["requirements"][1]["text"] = "Tolerate self-signed certificates on monitored hosts"
    proposal["coverage"][1] = {"req": "r2", "kind": "catalog", "cap": f"c{cap.id}"}
    st = [{"n": 1, "kind": "cap", "cap": cap, "role": "boards"}]
    return st, proposal, {"kind": "new", "stack": ["python"]}


def test_build_input_carries_what_the_conflict_needs(store):
    st, proposal, policy = _tls_inputs(store)
    item = kitplan.build_input(store, st, proposal, policy)["steps"][0]
    assert any("TLS verification disabled" in x for x in item["security"])
    assert item["requirements"] == ["Tolerate self-signed certificates on monitored hosts"]
    assert item["key_values"] == ["verify_tls: false"]


def test_a_security_conflict_becomes_a_decision_even_when_the_model_missed_it(store):
    st, proposal, policy = _tls_inputs(store)
    out = kitplan.plan(store, st, proposal, policy, runner=Runner({
        "steps": [{"n": 1, "in_scope": ["list boards"]}], "open_decisions": [
            {"id": "D1", "question": "Single board?", "options": ["yes", "no"],
             "default": "yes", "steps": [1]}]}))
    ds = out["plan"]["open_decisions"]
    assert [d["id"] for d in ds] == ["D1", "D2"]
    assert ds[1]["default"] == "Opt-in per item, off by default" and ds[1]["steps"] == [1]
    assert "Boards" in ds[1]["question"] and "self-signed" in ds[1]["question"]


def test_the_backstop_adds_nothing_when_a_decision_already_covers_it(store):
    st, proposal, policy = _tls_inputs(store)
    out = kitplan.plan(store, st, proposal, policy, runner=Runner({
        "steps": [{"n": 1}], "open_decisions": [
            {"id": "D1", "question": "Skip TLS verification for self-signed hosts?",
             "options": ["Verify certificates always", "Per-tile opt-in", "Skip for all"],
             "default": "Verify certificates always", "steps": [1]}]}))
    assert [d["id"] for d in out["plan"]["open_decisions"]] == ["D1"]


def test_the_backstop_replaces_the_last_model_decision_at_the_cap(store):
    st, proposal, policy = _tls_inputs(store)
    many = [{"id": f"D{i}", "question": f"Question {i}?", "options": ["a", "b"], "default": "a",
             "steps": [1]} for i in range(1, 7)]
    out = kitplan.plan(store, st, proposal, policy,
                       runner=Runner({"steps": [{"n": 1}], "open_decisions": many}))
    ds = out["plan"]["open_decisions"]
    assert len(ds) == 6 and ds[-1]["id"] == "D6" and "self-signed" in ds[-1]["question"]
    assert [d["question"] for d in ds[:5]] == [f"Question {i}?" for i in range(1, 6)]
    assert any("D6 dropped to make room" in p for p in out["problems"])


def test_no_conflict_no_backstop_decision(store):
    comp = make_comp(store)  # no insecure note anywhere
    cap = store.get_capability(comp.proposal["capabilities"][1]["capability_id"])
    cap.security_notes = "Never disable TLS verification."
    proposal = json.loads(json.dumps(comp.proposal))
    proposal["requirements"][1]["text"] = "Tolerate self-signed certificates"
    proposal["coverage"][1] = {"req": "r2", "kind": "catalog", "cap": f"c{cap.id}"}
    out = kitplan.plan(store, [{"n": 1, "kind": "cap", "cap": cap, "role": "b"}], proposal,
                       {"kind": "new", "stack": []}, runner=Runner({"steps": [{"n": 1}]}))
    assert out["plan"]["open_decisions"] == []  # the note forbids it, nothing to flag by rule


# -- phase 2: decisions, component contracts, lint -------------------------------------------
def _glue_steps():
    return [{"n": 0, "kind": "scaffold"},
            {"n": 1, "kind": "glue", "req": "r1", "text": "Show the board"}]


POLICY = {"kind": "new", "stack": ["Python", "FastAPI", "React"], "test_cmd": "pytest"}
ANSWERS = [{"id": "D1", "question": "May anonymous visitors view the board?",
            "answer": "Yes, viewing is public"}]


def test_phase_two_prompt_carries_the_decisions_and_ignores_new_open_decisions(store):
    reply = {**GOOD, "open_decisions": [{"id": "D9", "question": "Another?", "options": ["a", "b"],
                                         "default": "a", "steps": [1]}]}
    runner = Runner(reply)
    out = kitplan.plan(store, _glue_steps(), {}, POLICY, runner=runner, decisions=ANSWERS)
    assert out["plan"] is not None and out["plan"]["open_decisions"] == []
    prompt = runner.prompts[0]
    assert "These decisions are final" in prompt and "a public route has no UNAUTHORIZED" in prompt
    assert "Yes, viewing is public" in prompt and "May anonymous visitors view" in prompt
    phase1 = Runner(GOOD)
    kitplan.plan(store, _glue_steps(), {}, POLICY, runner=phase1)
    assert "These decisions are final" not in phase1.prompts[0]


def test_phase_two_cache_key_covers_the_answers(store):
    runner = Runner(GOOD)
    one = kitplan.plan(store, _glue_steps(), {}, POLICY, runner=runner)
    two = kitplan.plan(store, _glue_steps(), {}, POLICY, runner=runner, decisions=ANSWERS)
    assert one["key"] != two["key"] and len(runner.prompts) == 2
    again = kitplan.plan(store, _glue_steps(), {}, POLICY, runner=runner, decisions=ANSWERS)
    assert again["cached"] and again["key"] == two["key"] and len(runner.prompts) == 2
    changed = [{**ANSWERS[0], "answer": "No, sign-in is required"}]
    three = kitplan.plan(store, _glue_steps(), {}, POLICY, runner=runner, decisions=changed)
    assert three["key"] not in (one["key"], two["key"]) and len(runner.prompts) == 3
    unanswered = [{"id": "D2", "question": "q?", "answer": ""}]
    assert kitplan.plan(store, _glue_steps(), {}, POLICY, runner=runner,
                        decisions=unanswered)["key"] == one["key"]


COMPONENT = {"name": "TileGrid", "kind": "module", "consumers": [2], "fields": {
    "signature": "TileGrid(props) -> element",
    "props": [{"name": "tiles", "type": "Array<{id, x, y, w, h}>", "meaning": "tiles to draw",
               "required": True},
              {"name": "columns", "type": "number", "meaning": "grid columns", "required": "no"},
              {"meaning": "no name"}],
    "events": [{"name": "onChange", "payload": [{"name": "tiles", "type": "Tile[]",
                                                 "meaning": "the new layout"}]},
               {"name": "onSelect"}],
    "state": [{"name": "dragging", "type": "string | null", "meaning": "tile being dragged"}]}}


def test_component_contracts_are_validated_and_rendered():
    raw = {"steps": [{"n": 1, "provides": [COMPONENT, {
        "name": "formatDate", "kind": "module",
        "fields": {"props": [{"name": "x"}], "signature": "formatDate(d) -> str"}}]},
        {"n": 2, "uses": [{"name": "TileGrid", "from": 1}]}]}
    pl, _ = kitplan.validate(raw, _steps(), [])
    grid, helper = pl["steps"][0]["provides"]
    f = grid["fields"]
    assert [p["name"] for p in f["props"]] == ["tiles", "columns"]
    assert [p["required"] for p in f["props"]] == [True, False]
    assert f["events"][0]["payload"][0]["name"] == "tiles" and f["events"][1]["payload"] == []
    assert f["state"][0]["name"] == "dragging" and f["signature"]
    assert "props" not in helper["fields"]  # a plain function module is not a component
    text = "\n".join(kitplan.field_lines(grid))
    assert "`tiles` (Array<{id, x, y, w, h}>, required): tiles to draw" in text
    assert "`onChange` called with `tiles` (Tile[]): the new layout" in text
    assert "`onSelect` (no payload)" in text and "State: `dragging`" in text
    md = kitplan.contracts_md(pl, {1: "Grid"})
    assert "Props: `tiles`" in md and "Events: `onChange`" in md
    notes = "\n".join(kitplan.step_notes(pl, 2, "cap", {1: "Grid"}))
    assert "Props: `tiles`" in notes and "Events: `onChange`" in notes


def test_component_caps_and_ui_token():
    props = [{"name": f"p{i}", "type": "string"} for i in range(40)]
    events = [{"name": f"on{i}"} for i in range(15)]
    raw = {"steps": [{"n": 1, "provides": [
        {"name": "tile-grid", "kind": "module", "token": "ui:tile-grid",
         "fields": {"props": props, "events": events}}]}]}
    pl, _ = kitplan.validate(raw, _steps(), [])
    f = pl["steps"][0]["provides"][0]["fields"]
    assert len(f["props"]) == kitplan.MAX_PROPS and len(f["events"]) == kitplan.MAX_EVENTS
    assert kitplan.is_component("TileGrid component") and not kitplan.is_component("Board")
    assert not kitplan.is_component("require_admin(request)")


def _lint_plan(api_style, provides=(), in_scope=()):
    return {"api_style": api_style, "access_model": [], "settings": [], "open_decisions": [],
            "steps": [{"n": 1, "in_scope": list(in_scope), "out_of_scope": [], "uses": [],
                       "checks": [], "library_note": "", "provides": list(provides)}]}


def test_lint_reports_fields_that_break_the_claimed_convention():
    rec = {"name": "Tile", "kind": "record", "token": "", "consumers": [], "fields": {"fields": [
        {"name": "id"}, {"name": "service_id"}, {"name": "skipTlsVerification"},
        {"name": "tileWidth"}]}}
    out = kitplan.lint(_lint_plan("REST + JSON, snake_case field names", [rec]), POLICY, {})
    assert len(out) == 1 and out[0]["step"] == 1
    msg = out[0]["message"]
    assert "snake_case" in msg and "camelCase" in msg and "`skipTlsVerification`" in msg
    assert "`service_id`" not in msg and "exactly as listed" in msg
    assert kitplan.lint(_lint_plan("REST + JSON, camelCase", [rec]), POLICY, {})[0][
        "message"].count("`") == 2  # only service_id breaks camelCase
    assert kitplan.lint(_lint_plan("REST + JSON", [rec]), POLICY, {}) == []
    assert kitplan.lint(_lint_plan("snake_case columns, camelCase JSON", [rec]), POLICY, {}) == []


def test_lint_reports_libraries_outside_the_stack():
    pl = _lint_plan("REST", in_scope=["Hash passwords with bcrypt"])
    out = kitplan.lint(pl, POLICY, {1: "Use `passlib` and httpx and pydantic. Send a request."})
    msgs = [(o["step"], o["message"]) for o in out]
    assert (1, "Spec mentions bcrypt, which is not in the allowed set: use the standard library "
                "or the stack (for example hashlib.scrypt) or log it in DEPENDENCIES.md with "
                "its reason.") in msgs
    flagged = " ".join(m for _, m in msgs)
    assert "passlib" in flagged and "httpx" not in flagged and "pydantic" not in flagged
    assert sum("bcrypt" in m for _, m in msgs) == 1
    unknown = kitplan.lint(_lint_plan("REST"), POLICY, {2: "Drag with react-grid-layout"})
    assert "(for example" not in unknown[0]["message"] and unknown[0]["step"] == 2


def test_lint_has_no_false_positives_on_common_words():
    spec_text = ("The client requests a board; the request is validated. Use moment of silence. "
                 "A bcryptish thing, my-bcrypt-fork and uuid values are fine as words. "
                 "react-dom comes with React.")
    assert kitplan.lint(_lint_plan("REST"), POLICY, {1: spec_text}) == []
    assert kitplan.lint(_lint_plan("REST"), POLICY, {1: "pip install requests"})
    assert kitplan.lint(_lint_plan("REST"), {"stack": []}, {1: "Use bcrypt"}) == []


def test_setting_defaults_keep_local_values_and_never_leave_a_marker():
    sd = kitplan.setting_default
    for keep in ["./data/app.db", "data/tunes.db", "~/.webarchive", "http://127.0.0.1:11434",
                 "http://localhost:4780/api", "5 MiB", "30"]:
        assert sd(keep) == keep
    assert sd("`http://localhost:11434`") == "http://localhost:11434"
    assert sd(True) == "true"
    for gone in ["[value unknown: size limit]", "src/app/db.py", "https://github.com/x/y",
                 "/Users/dan/x"]:
        assert sd(gone) == kitplan.NO_DEFAULT

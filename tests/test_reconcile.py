"""Kit v0.7 slice B: the reconcile pass (reconcile.py and its place in export_kit)."""

import json

from reincarnate import claude_runner, estimate, kit, paths, reconcile, salvage
from reincarnate.models import Composition, Profile
from reincarnate.prompts import UNTRUSTED_DATA_RULE

PLAN = {
    "api_style": "REST + JSON under /api; the session cookie is named session",
    "access_model": ["Only the owner signs in"],
    "settings": [{"key": "SESSION_DAYS", "step": 1, "meaning": "lifetime [value unknown: days]"},
                 {"key": "SECRET", "step": 1, "meaning": "signs cookies"}],
    "steps": [
        {"n": 0, "provides": [{"name": "authorize(request)", "kind": "module", "consumers": [1]}]},
        {"n": 1, "in_scope": ["owner sign-in"], "out_of_scope": ["invites", "password reset"],
         "provides": [{"name": "session cookie", "kind": "setting", "consumers": [2]}]},
        {"n": 2, "in_scope": ["one board"], "out_of_scope": ["rename and duplicate boards"],
         "uses": [{"name": "session cookie", "from": 1}]},
    ],
    "open_decisions": [
        {"id": "D1", "question": "How many days does a session last?", "why": "cookie lifetime",
         "options": ["7", "30"], "default": "30", "steps": [1]}],
}
REPLY = {
    "steps": [
        {"n": 1, "checks": [{"i": 0, "action": "rewrite",
                             "text": "A session expires after 7 days",
                             "reason": "decision D1 says 7 days"},
                            {"i": 1, "action": "drop", "reason": "invites are out of scope"},
                            {"i": 2, "action": "keep"}],
         "key_values": [{"i": 0, "action": "rewrite", "value": "session",
                         "reason": "the contract names the cookie session"}]},
        {"n": 2, "checks": [{"i": 0, "action": "drop", "reason": "single board"}]},
    ],
    "settings": [{"key": "SESSION_DAYS", "default": "7", "reason": "D1 answered 7"}],
}


def seed(store):
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", {
        "summary": "A tool.", "dependencies": {"license": "MIT"},
        "capabilities": [
            {"name": "Auth", "what": "Local sign-in.", "portability": "high",
             "files": ["src/auth.py"], "provides": ["auth:session"],
             "acceptance": ["A session lasts 30 days", "An owner can invite a user",
                            "Login sets a cookie"],
             "key_values": [{"name": "cookie", "value": "next-auth.session-token",
                             "meaning": "session cookie"}]},
            {"name": "Boards", "what": "Lists boards.", "portability": "high",
             "files": ["src/boards.py"], "requires": ["auth:session"],
             "acceptance": ["A board can be renamed", "Boards are listed"]}]})
    return {c.name: c.id for c in store.list_capabilities()}


def make_comp(store):
    ids = seed(store)
    proposal = {
        "capabilities": [{"capability_id": ids["Auth"], "order": 1, "name": "Auth",
                          "donor": "own/repo"},
                         {"capability_id": ids["Boards"], "order": 2, "name": "Boards",
                          "donor": "own/repo"}],
        "stack": "Python", "gaps": [], "requirements": [], "coverage": [], "glue": "",
        "wiring": {"wired": []}}
    return store.save_composition(Composition(
        slug="app", name="App", target_kind="new", target_slug="fresh", proposal=proposal))


class Runner:
    def __init__(self, reply, cost=0.05):
        self.reply, self.cost, self.prompts = reply, cost, []

    def __call__(self, prompt, model):
        self.prompts.append(prompt)
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return claude_runner.RunResult(text=text, cost_usd=self.cost)


def kd():
    return paths.data_dir() / "new" / "fresh" / ".reincarnate"


def spec(prefix):
    return next((kd() / "specs").glob(f"{prefix}-*.md")).read_text()


def export(store, comp, rec, answers=None, **kw):
    return kit.export_kit(store, comp, "new:fresh", plan_runner=Runner(PLAN),
                          reconcile_runner=rec, answers=answers or {"D1": "7"}, **kw)


def test_conflicts_are_rewritten_dropped_and_listed_with_reasons(store):
    comp = make_comp(store)
    rec = Runner(REPLY)
    out = export(store, comp, rec)
    auth, boards = spec("01"), spec("02")
    body = auth.split("**Changed to match")[0]
    assert "- A session expires after 7 days" in body and "A session lasts 30 days" not in body
    assert "invite a user" not in body and "- Login sets a cookie" in body
    assert "- cookie: session" in body and "next-auth.session-token" not in body
    tail = auth.split("**Changed to match the decisions and contracts:**")[1]
    assert "Removed check: An owner can invite a user (reason: invites are out of scope)" in tail
    assert "Rewrote check: A session lasts 30 days -> A session expires after 7 days" in tail
    assert "Rewrote key value: cookie: next-auth.session-token -> cookie: session" in tail
    assert auth.rstrip().endswith("the contract names the cookie session)")
    assert "A board can be renamed" not in boards.split("**Changed to match")[0]
    assert "Removed check: A board can be renamed (reason: single board)" in boards
    assert "**Changed to match" not in spec("00")
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert "[value unknown" not in contracts and "Default: 7." in contracts
    assert "signs cookies" in contracts
    assert out["reconcile"] == {"status": "used", "note": "", "cached": False,
                                "cost_usd": 0.05, "changes": 5}
    assert out["reconcile_cost"] == 0.05 and out["plan_cost"] == 0.05
    assert any("changed 5 line(s)" in w for w in out["warnings"])
    manifest = json.loads((kd() / "manifest.json").read_text())
    assert manifest["reconcile"]["model"] == reconcile.MODEL
    assert manifest["reconcile"]["data"]["settings"][0]["key"] == "SESSION_DAYS"
    assert manifest["plan"]["data"]["settings"][0]["meaning"].endswith("[value unknown: days]")


def test_prompt_is_catalog_text_with_the_rule_and_the_answers(store):
    comp = make_comp(store)
    rec = Runner(REPLY)
    export(store, comp, rec)
    prompt = rec.prompts[0]
    assert UNTRUSTED_DATA_RULE in prompt and prompt.index(UNTRUSTED_DATA_RULE) < prompt.index("{")
    assert "src/auth.py" not in prompt and "boards.py" not in prompt
    assert "next-auth.session-token" in prompt and "rename and duplicate boards" in prompt
    assert '"answer": "7"' in prompt and "authorize(request)" in prompt


def test_cache_means_no_second_call_and_a_changed_answer_makes_one(store):
    comp = make_comp(store)
    rec = Runner(REPLY)
    export(store, comp, rec)
    again = export(store, store.get_composition("app"), rec, force=True)
    assert len(rec.prompts) == 1
    assert again["reconcile"]["cached"] is True and again["reconcile"]["status"] == "cached"
    assert again["reconcile"]["changes"] == 5 and again["reconcile_cost"] == 0.0
    assert "A board can be renamed" not in spec("02").split("**Changed to match")[0]
    export(store, store.get_composition("app"), rec, answers={"D1": "30"}, force=True)
    assert len(rec.prompts) == 2


def test_dry_run_preview_shows_the_reconciled_specs_and_shares_the_cache(store):
    comp = make_comp(store)
    rec = Runner(REPLY)
    out = export(store, comp, rec, dry_run=True)
    assert out["reconcile"]["changes"] == 5
    assert "Rewrote check" in out["files"]["specs/01-auth.md"]
    assert not kd().exists()
    export(store, comp, rec)
    assert len(rec.prompts) == 1  # the real export reused the preview's call


def test_failures_fall_back_to_the_unreconciled_kit(store):
    for reply, why in ((("not json at all"), "no JSON object"), ([1, 2], "no JSON object")):
        comp = make_comp(store) if not store.list_compositions() else \
            store.get_composition("app")
        out = export(store, comp, Runner(reply), force=True)
        assert out["reconcile"]["status"] == "skipped" and out["reconcile"]["changes"] == 0
        assert any(w.startswith("reconcile pass skipped:") and why in w for w in out["warnings"])
        assert "A board can be renamed" in spec("02") and "**Changed to match" not in spec("02")
        assert "[value unknown: days]" in (kd() / "CONTRACTS.md").read_text()

    def boom(prompt, model):
        if "editor of a software kit" in prompt:
            raise RuntimeError("down")
        return claude_runner.RunResult(text=json.dumps(PLAN), cost_usd=0.05)

    out = kit.export_kit(store, store.get_composition("app"), "new:fresh", plan_runner=boom,
                         reconcile_runner=boom, answers={"D1": "7"}, force=True)
    assert out["files"] and "model call failed: RuntimeError" in out["reconcile"]["note"]


def test_missing_claude_is_a_quiet_note(store):
    comp = make_comp(store)

    def missing(prompt, model):
        return claude_runner.RunResult(rc=127, stderr="no claude")

    out = export(store, comp, missing)
    assert out["reconcile"]["status"] == "skipped"
    assert out["reconcile"]["note"] == "reconcile pass not run: claude is not available"
    assert not [w for w in out["warnings"] if "reconcile" in w]


def test_no_plan_skips_both_passes(store):
    comp = make_comp(store)
    rec = Runner(REPLY)
    out = kit.export_kit(store, comp, "new:fresh", plan=False, reconcile_runner=rec)
    assert rec.prompts == [] and out["reconcile"]["status"] == "off"
    assert "--no-plan" in out["reconcile"]["note"]
    assert "A board can be renamed" in spec("02")


def test_validate_rejects_bad_indices_empty_rewrites_and_additions():
    data = {"settings": [{"key": "K", "step": 1, "meaning": ""}],
            "steps": [{"n": 1, "checks": [{"i": 0, "text": "a"}, {"i": 1, "text": "b"}],
                       "key_values": [{"i": 0, "name": "x", "value": "1"}]}]}
    raw = {"steps": [
        {"n": 9, "checks": [{"i": 0, "action": "drop"}]},
        {"n": 1,
         "checks": [{"i": 5, "action": "drop"}, {"i": -1, "action": "drop"},
                    {"i": 0, "action": "rewrite", "text": "  "},
                    {"i": 1, "action": "rewrite", "text": "see ```rm -rf /``` and /etc/passwd",
                     "reason": "r"},
                    {"i": 1, "action": "drop"}, {"i": 2, "action": "add", "text": "new"}],
         "key_values": [{"i": 1, "action": "drop"}, {"i": 0, "action": "keep"}]}],
        "settings": [{"key": "NOPE", "default": "1"}, {"key": "K", "default": ""},
                     {"key": "k", "default": "3 days"}]}
    result, problems = reconcile.validate(raw, data)
    assert [e["n"] for e in result["steps"]] == [1]
    only = result["steps"][0]
    assert [c["i"] for c in only["checks"]] == [1] and only["key_values"] == []
    text = only["checks"][0]["text"]
    assert "rm -rf" not in text and "/etc/passwd" not in text
    assert result["settings"] == [{"key": "K", "default": "3 days", "reason": ""}]
    joined = " ".join(problems)
    assert "unknown step 9" in joined and "index 5" in joined and "empty rewrite" in joined
    assert "check index 2" in joined and "NOPE" in joined
    assert reconcile.validate([], data)[0] is None


def test_rewritten_text_is_gated_in_the_spec(store):
    comp = make_comp(store)
    reply = {"steps": [{"n": 2, "checks": [{"i": 0, "action": "rewrite",
                                            "text": "Boards load from `open('/etc/x')` first",
                                            "reason": "see /home/dan/secret.py"}]}]}
    export(store, comp, Runner(reply))
    boards = spec("02")
    assert "/etc/x" not in boards and "/home/dan" not in boards


def test_estimate_reconcile_grows_with_steps():
    a, b = estimate.estimate_reconcile(2), estimate.estimate_reconcile(8)
    assert a["low"] < a["high"] and b["high"] > a["high"]
    assert a["low"] == round((0.04 + 0.01 * 2) * 0.5, 4)


# -- kit v0.8 slice B: re-plan after the decisions, prose reconcile, tidy, lints ----------------
PLAN2 = {**PLAN, "api_style": "REST + JSON under /api; sessions last 7 days, cookie named session",
         "settings": [*PLAN["settings"], {"key": "TRUST_PROXY", "step": 1, "meaning": "proxy"}],
         "steps": [PLAN["steps"][0],
                   {**PLAN["steps"][1], "provides": [
                       {"name": "session cookie", "kind": "setting", "consumers": [2]},
                       {"name": "isOwner(request)", "kind": "module", "consumers": [2]}]},
                   PLAN["steps"][2]],
         "open_decisions": [{"id": "D9", "question": "Another?", "options": ["a", "b"],
                             "default": "a", "steps": [1]}]}


class TwoPhase(Runner):
    """A plan runner: PLAN for the first pass, PLAN2 once the prompt carries the decisions."""
    def __init__(self, second=PLAN2, cost=0.05):
        super().__init__(PLAN, cost)
        self.second = second

    def __call__(self, prompt, model):
        self.prompts.append(prompt)
        if "decisions are final" in prompt:
            if isinstance(self.second, Exception):
                raise self.second
            return claude_runner.RunResult(text=json.dumps(self.second), cost_usd=self.cost)
        return claude_runner.RunResult(text=json.dumps(self.reply), cost_usd=self.cost)


def test_answers_trigger_a_second_plan_whose_contracts_replace_the_first(store):
    comp = make_comp(store)
    plan_runner, rec = TwoPhase(), Runner({"steps": []})
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=plan_runner,
                         reconcile_runner=rec, answers={"D1": "7"})
    assert len(plan_runner.prompts) == 2 and "decisions are final" in plan_runner.prompts[1]
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert "sessions last 7 days" in contracts and "TRUST_PROXY" in contracts
    assert "isOwner(request)" in spec("01")  # the spec's Provides notes come from the new plan
    assert [d["id"] for d in out["decisions"]] == ["D1"]  # the first plan's decision list
    assert out["decisions"][0]["options"] == ["7", "30"] and "D9" not in contracts
    assert out["plan2"] == {"status": "used", "cached": False, "cost_usd": 0.05}
    assert out["plan_cost"] == 0.05 and out["plan2_cost"] == 0.05
    manifest = json.loads((kd() / "manifest.json").read_text())
    assert manifest["plan"]["data"]["api_style"] == PLAN["api_style"]
    assert manifest["plan2"]["data"]["api_style"] == PLAN2["api_style"]
    assert set(manifest["plan2"]) == {"key", "model", "cost_usd", "data"}
    assert "TRUST_PROXY" in rec.prompts[0] and "isOwner(request)" in rec.prompts[0]


def test_plan2_is_cached_and_an_answer_change_reruns_only_phase_two_and_reconcile(store):
    comp = make_comp(store)
    plan_runner, rec = TwoPhase(), Runner(REPLY)
    kw = {"plan_runner": plan_runner, "reconcile_runner": rec, "force": True}
    kit.export_kit(store, comp, "new:fresh", answers={"D1": "7"}, **kw)
    again = kit.export_kit(store, store.get_composition("app"), "new:fresh", **kw)
    assert len(plan_runner.prompts) == 2 and len(rec.prompts) == 1
    assert again["plan2"]["status"] == "cached" and again["plan2"]["cost_usd"] == 0.0
    kit.export_kit(store, store.get_composition("app"), "new:fresh", answers={"D1": "30"}, **kw)
    assert len(plan_runner.prompts) == 3 and "decisions are final" in plan_runner.prompts[2]
    assert len(rec.prompts) == 2


def test_no_answers_means_no_second_plan(store):
    comp = make_comp(store)
    plan_runner = TwoPhase()
    out = kit.export_kit(store, comp, "new:fresh", plan_runner=plan_runner,
                         reconcile_runner=Runner({"steps": []}))
    assert len(plan_runner.prompts) == 1 and out["plan2"]["status"] == "off"
    assert "plan2" not in json.loads((kd() / "manifest.json").read_text())


def test_a_failed_second_plan_keeps_the_first_contracts_and_warns(store):
    comp = make_comp(store)
    for second in (RuntimeError("down"), "not a plan"):
        plan_runner = TwoPhase(second)
        out = kit.export_kit(store, comp, "new:fresh", plan_runner=plan_runner,
                             reconcile_runner=Runner({"steps": []}), answers={"D1": "7"},
                             force=True)
        assert out["plan2"]["status"] == "skipped" and out["files"]
        assert any(w.startswith("contracts were not re-planned after the decisions: ")
                   for w in out["warnings"])
        contracts = (kd() / "CONTRACTS.md").read_text()
        assert PLAN["api_style"] in contracts and "TRUST_PROXY" not in contracts
        assert "plan2" not in json.loads((kd() / "manifest.json").read_text())


def test_prose_rewrite_drop_and_keep_change_the_spec(store):
    comp = make_comp(store)
    reply = {"steps": [
        {"n": 1, "prose": [{"i": 0, "action": "rewrite", "reason": "invites are out of scope",
                            "text": "Owner sign-in with a cookie; see `open('/etc/x')`"}]},
        {"n": 2, "prose": [{"i": 0, "action": "drop", "reason": "single board"}]}]}
    rec = Runner(reply)
    export(store, comp, rec)
    data = json.loads(rec.prompts[0].split("Kit data (JSON):\n")[1].split("\n\nReturn ONLY")[0])
    assert data["steps"][1]["prose"] == [{"i": 0, "kind": "what", "text": "Local sign-in."}]
    auth, boards = spec("01"), spec("02")
    body = auth.split("**Changed to match")[0]
    assert "Owner sign-in with a cookie" in body and "Local sign-in." not in body
    assert "/etc/x" not in auth  # replacement text is gated
    assert ("- Rewrote the behavior description to match the scope: invites are out of scope"
            in auth)
    assert "Owner sign-in with a cookie; see" not in auth.split("**Changed to match")[1]
    assert "_(no description recorded)_" in boards and "Lists boards." not in boards
    assert "- Removed the behavior description (reason: single board)" in boards
    keep = {"steps": [{"n": 1, "prose": [{"i": 0, "action": "keep"}]}]}
    export(store, store.get_composition("app"), Runner(keep), answers={"D1": "30"}, force=True)
    assert "Local sign-in." in spec("01") and "**Changed to match" not in spec("01")


def test_prose_validation_ignores_bad_entries():
    data = {"settings": [], "steps": [{"n": 1, "checks": [], "key_values": [],
                                       "prose": [{"i": 0, "kind": "what", "text": "t"},
                                                  {"i": 1, "kind": "intent", "text": "u"}]}]}
    raw = {"steps": [{"n": 1, "prose": [{"i": 3, "action": "drop"},
                                        {"i": 0, "action": "rewrite", "text": " "},
                                        {"i": 1, "action": "rewrite", "text": "new",
                                         "reason": "r"}]}]}
    result, problems = reconcile.validate(raw, data)
    assert result["steps"][0]["prose"] == [{"i": 1, "action": "rewrite", "reason": "r",
                                            "text": "new"}]
    assert "prose index 3" in " ".join(problems) and "empty rewrite of prose" in " ".join(problems)
    assert reconcile.PROMPT_REV != "reconcile-1"


def test_lint_messages_reach_contracts_specs_and_warnings(store, monkeypatch):
    comp = make_comp(store)
    seen = {}

    def lint(plan, policy, spec_texts):
        seen["texts"] = spec_texts
        return [{"step": None, "message": "The API style says snake_case but shapes use camelCase"},
                {"step": 1, "message": "bcrypt is not in the allowed dependencies"}]

    monkeypatch.setattr(kit.kitplan, "lint", lint, raising=False)
    out = export(store, comp, Runner(REPLY))
    assert set(seen["texts"]) == {0, 1, 2} and "Local sign-in" in seen["texts"][1]
    contracts = (kd() / "CONTRACTS.md").read_text()
    assert contracts.rstrip().endswith("- The API style says snake_case but shapes use camelCase")
    assert "\n## Notes\n" in contracts
    auth = spec("01")
    assert "**Contract notes:**\n\n- bcrypt is not in the allowed dependencies" in auth
    assert "Contract notes" not in spec("02")
    assert any("bcrypt" in w for w in out["warnings"])
    assert any("snake_case" in w for w in out["warnings"])


def test_a_missing_or_failing_lint_never_blocks(store, monkeypatch):
    comp = make_comp(store)
    monkeypatch.delattr(kit.kitplan, "lint", raising=False)
    out = export(store, comp, Runner(REPLY))
    assert "## Notes" not in (kd() / "CONTRACTS.md").read_text() and out["files"]

    def boom(plan, policy, spec_texts):
        raise ValueError("bad")

    monkeypatch.setattr(kit.kitplan, "lint", boom, raising=False)
    out = export(store, store.get_composition("app"), Runner(REPLY), force=True)
    assert any("contract lints skipped" in w for w in out["warnings"])


# -- tidy_removed ---------------------------------------------------------------------------
def test_tidy_removed_handles_the_real_v6_strings():
    t = salvage.tidy_removed
    assert (t("The LDAP integration (~200 lines in [path removed]) is a well-structured "
              "reference") == "The LDAP integration is a well-structured reference")
    assert (t("The tRPC middleware tier (`publicProcedure` / `protectedProcedure` / "
              "`adminProcedure` in [path removed]) is a clean pattern")
            == "The tRPC middleware tier (`publicProcedure` / `protectedProcedure` / "
               "`adminProcedure`) is a clean pattern")
    assert (t("Pings apps (see [path removed]). Built on Next.js.")
            == "Pings apps. Built on Next.js.")
    assert t("Read it (e.g. [code removed]) first") == "Read it first"
    assert t("Defined in [path removed], used twice.") == "Defined, used twice."
    assert t("Defined in [path removed].") == "Defined."


def test_tidy_removed_keeps_a_marker_that_carries_the_sentence():
    t = salvage.tidy_removed
    for text in ("In [path removed].", "See [path removed].", "- [code removed]",
                 "Loads config from [path removed] on start", "no markers here (see below)",
                 "  - indented  item"):
        assert t(text) == text
    assert t("A (see [path removed]) b\n  - x  (in [path removed])\n") == "A b\n  - x\n"
    for text in ("A (see [path removed]) b", "x in [path removed]."):
        assert "()" not in t(text) and " ." not in t(text) and "in ." not in t(text)


def test_overrides_reach_build_clean_room_spec_gated(store):
    make_comp(store)
    cap = next(c for c in store.list_capabilities() if c.name == "Auth")
    text = salvage.build_clean_room_spec(
        store, cap, target_slug="new:x", what="Signs in (see src/auth.py) with `open('/x')`",
        design_notes="", intent="Use it from the board")
    assert "Signs in with [code removed]" in text and "src/auth.py)" not in text.split(
        "**Donor reference")[0]
    assert "Design notes" not in text and "Integration intent (from the dossier):** Use it" in text
    plain = salvage.build_clean_room_spec(store, cap, target_slug="new:x")
    assert "Local sign-in." in plain

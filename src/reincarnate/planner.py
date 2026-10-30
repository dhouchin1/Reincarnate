"""The idea-first kit planner: two Opus calls from the idea, the stack and the target, with no
catalog part in either prompt. The scope call returns the requirements and the open product
decisions (the user answers them with `--decisions`/`--answers`); the plan call returns the
contracts and one spec per step."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, claude_runner, kitplan, salvage, schema

MODEL = "claude-opus-5-5"
MAX_DECISIONS = 8
PROMPT_REV = "idea-1"
PLANNERS = ("idea", "parts")
DEFAULT = "idea"
QUESTION_CAP = 400
OPTION_CAP = 200
REQ_CAP = 400
SPEC_CAP = 30000
CONTRACTS_CAP = 60000
TITLE_CAP = 120

SCOPE_SCHEMA = (
    '{"requirements": [""], "decisions": [{"id": "D1", "question": "", "why": "", '
    '"options": ["", ""], "default": ""}]}')
PLAN_SCHEMA = '{"contracts_md": "", "steps": [{"n": 0, "title": "", "spec_md": ""}]}'
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def choose(value: str | None = None) -> str:
    """The planner to use: the argument, else $REINCARNATE_PLANNER, else the default."""
    which = (value or os.environ.get("REINCARNATE_PLANNER") or DEFAULT).strip().lower()
    if which not in PLANNERS:
        raise ValueError(f"unknown planner {which!r}; use one of: {', '.join(PLANNERS)}")
    return which


def planner_input(composition, policy: dict) -> dict:
    """What the planner may see: the name, the idea, the stack and the target. Plain data; the
    composition's catalog parts are never part of it."""
    stack = list(policy.get("stack") or [])
    text = str((composition.proposal or {}).get("stack") or "").strip()
    new = policy.get("kind") == "new"
    return {"name": _line(composition.name, 120), "idea": _text(composition.idea).strip(),
            "stack_text": text if new and text else ", ".join(stack), "stack": stack,
            "ecosystems": list(policy.get("ecosystems") or []),
            "test_cmd": str(policy.get("test_cmd") or ""), "kind": policy.get("kind") or "new"}


def _text(v) -> str:
    return _CTRL.sub("", str(v or ""))


def _line(v, n: int) -> str:
    return schema.clean_line(v, n, backticks=True)


def _block(v, cap: int) -> str:
    """Multi-line markdown with control characters removed, clipped at `cap`. Not path-gated:
    the planner never sees donor text, and its own project paths must stay."""
    return _text(v).strip()[:cap].rstrip()


# -- the scope call ----------------------------------------------------------------------------
def _facts(inp: dict) -> str:
    multi = (" It is `make test`, so step 0 writes a Makefile whose test target runs every "
             "part's tests." if inp["test_cmd"] == salvage.MULTI_TEST_CMD else "")
    new = inp["kind"] == "new"
    test = (f"`{inp['test_cmd']}`" + (". Step 0 makes it run and pass." if new else "")
            if inp["test_cmd"] else "(none set; follow the project's own)")
    return (f"App: {inp['name']}\nIdea: {inp['idea']}\n"
            f"Stack: {inp['stack_text'] or '(follow the existing project)'}\n"
            f"Target: {'a new project' if new else 'an existing project'}\n"
            f"Test command: {test}{multi if new else ''}")


def scope_prompt(inp: dict) -> str:
    return f"""\
You are scoping a software kit before it is planned. A later pass turns your requirements and \
the user's answers to your decisions into build steps, so do not plan steps here.

{_facts(inp)}

Rules:
- requirements: the app's requirements as short testable sentences, derived only from the idea. \
Do not add features the idea does not imply.
- decisions: up to {MAX_DECISIONS} real PRODUCT choices the idea leaves open, the ones the user \
should make (who can see or edit what, what happens to old data, whether a feature is on by \
default). Not technical choices a builder can make (library, file layout, naming). Each has an \
id "D1", "D2", ..., a one-sentence question, why it matters, 2-4 options and a default that is \
one of the options (the safest and simplest). If the idea leaves nothing open, return no \
decisions.

Return ONLY a JSON object, no prose:
{SCOPE_SCHEMA}"""


def validate_scope(raw) -> tuple[dict | None, list[str]]:
    """`{requirements, decisions}` with the bad decisions dropped (each reason in the problem
    list) and the kept ones renumbered D1..Dn, or None when there are no requirements."""
    if not isinstance(raw, dict):
        return None, ["the reply is not a JSON object"]
    problems: list[str] = []
    reqs: list[str] = []
    for r in raw.get("requirements") if isinstance(raw.get("requirements"), list) else []:
        t = _line(r, REQ_CAP) if isinstance(r, (str, int, float)) else ""
        if t and t not in reqs:
            reqs.append(t)
    if not reqs:
        return None, ["no requirements"]
    out: list[dict] = []
    for i, d in enumerate(raw.get("decisions") if isinstance(raw.get("decisions"), list) else []):
        label = (str(d.get("id") or "").strip() if isinstance(d, dict) else "") or f"#{i + 1}"
        if not isinstance(d, dict):
            problems.append(f"open decision {label} dropped: not an object")
            continue
        question = _line(d.get("question"), QUESTION_CAP)
        options: list[str] = []
        for o in d.get("options") if isinstance(d.get("options"), list) else []:
            t = _line(o, OPTION_CAP) if isinstance(o, (str, int, float)) else ""
            if t and t.casefold() not in [x.casefold() for x in options]:
                options.append(t)
        options = options[:4]
        default = _line(d.get("default"), OPTION_CAP).casefold()
        default = next((o for o in options if o.casefold() == default), "")
        why = None
        if not question:
            why = "no question"
        elif len(options) < 2 or not default:
            why = "fewer than two options or a default that is not one of them"
        elif len(out) >= MAX_DECISIONS:
            why = f"more than {MAX_DECISIONS} open decisions"
        if why:
            problems.append(f"open decision {label} dropped: {why}")
            continue
        out.append({"id": f"D{len(out) + 1}", "question": question,
                    "why": _line(d.get("why"), QUESTION_CAP), "options": options,
                    "default": default, "steps": []})
    return {"requirements": reqs, "decisions": out}, problems


# -- the plan call -----------------------------------------------------------------------------
def _answer_text(d: dict) -> str:
    if d.get("answer"):
        return f"Decided: {d['answer']}"
    return f"not answered, use the default: {d['default']}"


def plan_prompt(inp: dict, scope_data: dict, decided: list[dict]) -> str:
    new = inp["kind"] == "new"
    reqs = "\n".join(f"- {r}" for r in scope_data["requirements"])
    decs = "\n".join(f"- {d['id']}. {d['question']} ({_answer_text(d)})" for d in decided)
    if new:
        first = (
            "- steps: numbered from 0, foundational first, as many as the app needs. Each step is "
            "one reviewable change a single session can build and test. Step 0 is the scaffold: "
            "package or module name, entry point, language version, source layout and the test "
            "setup. Every later step uses that layout.")
        pins = salvage.version_lines(inp["stack"], inp["ecosystems"], inp["test_cmd"])
        if pins:
            first += ("\n- Step 0 must state these, as requirements of the scaffold:\n"
                      + "\n".join(f"  {x}" for x in pins))
    else:
        first = (
            "- steps: numbered from 1, foundational first, as many as the change needs. There is "
            "no scaffold step: the project exists. Follow its conventions (layout, naming, test "
            "style) and add to it; do not restructure it.")
    return f"""\
You are planning a software kit: an ordered list of build steps. Each step is built later by a \
separate coding session with no other context. That session reads .reincarnate/CLAUDE.md (the \
stack, the test command and the rules), .reincarnate/CONTRACTS.md, .reincarnate/tasks.md and its \
own step's spec, then builds only that step. Plan so that no builder has to guess.

{_facts(inp)}

Requirements:
{reqs}

Decisions (final; the plan must follow each one as decided, or its default when unanswered):
{decs or '(none)'}

Rules:
{first}
- contracts_md: the markdown body of CONTRACTS.md, fixed once for the whole kit: the API style \
(paths, naming convention for fields, error shape, auth guard), who may view, edit and \
administer, every setting with its key, type, concrete default and owning step, and every \
route, record, type, module or UI component a step provides, with its exact fields, request \
and response shapes, error codes or signatures, the step that provides it and the steps that \
use it. Each provided thing has exactly one owning step. Do not write a Decisions section; the \
decisions above are rendered into it.
- spec_md for each step: markdown with the goal, in scope, out of scope, the behavior, the data \
shapes and key values it needs, what it provides and what it uses from earlier steps (by the \
names in contracts_md), and 2-6 acceptance checks with concrete values. End every spec with the \
line "**Acceptance checks:**" followed by one "- " bullet per check. A spec agrees with \
contracts_md exactly.
- Use only the stack's libraries or the standard library.
- Every setting has a concrete default that suits a local, single-user install.
- Pin the language version. A Python test command runs `.venv/bin/python -m pytest`, never the \
machine's python3.
- When the stack has a UI test library, list its peer dev dependencies in step 0 \
(`@testing-library/dom`, `@testing-library/user-event`, `@types/react` and the like), and when \
tests run in jsdom (vitest), step 0 sets up the shims the library needs (PointerEvent, \
ResizeObserver, localStorage and sessionStorage).
- A step that replaces a placeholder (a stub page, a fake handler) also updates the earlier \
tests the replacement breaks.
- When there is a backend and a frontend, the final step makes the backend serve the built \
frontend, so one command runs the whole app.
- Give the UI enough steps that every user-visible requirement is reachable in the browser.

Return ONLY a JSON object, no prose:
{PLAN_SCHEMA}"""


def validate_plan(raw, first: int) -> tuple[dict | None, list[str]]:
    """`{contracts_md, steps}`, or None with the reasons. Steps run first, first+1, ... in order,
    each with a title and a spec, and the contracts are not empty."""
    if not isinstance(raw, dict):
        return None, ["the reply is not a JSON object"]
    problems = []
    steps = [s for s in raw.get("steps") or [] if isinstance(s, dict)]
    if not steps:
        problems.append("no steps")
    for i, s in enumerate(steps, first):
        if s.get("n") != i or isinstance(s.get("n"), bool):
            problems.append(f"step {i} is numbered {s.get('n')!r}")
        if not _line(s.get("title"), TITLE_CAP) or not _block(s.get("spec_md"), SPEC_CAP):
            problems.append(f"step {i} has no title or spec")
    contracts = _block(raw.get("contracts_md"), CONTRACTS_CAP)
    if not contracts:
        problems.append("no contracts_md")
    if problems:
        return None, problems
    return {"contracts_md": contracts,
            "steps": [{"n": s["n"], "title": _line(s["title"], TITLE_CAP),
                       "spec_md": _block(s["spec_md"], SPEC_CAP)} for s in steps]}, []


# -- running the calls -------------------------------------------------------------------------
def _default_runner(label: str):
    def run(prompt: str, model: str):
        with tempfile.TemporaryDirectory(prefix="reincarnate-planner-") as tmp:
            return claude_runner.run(
                prompt, cwd=Path(tmp), model=model, max_turns=1, tools="", timeout=1800,
                join_turns=True, env_extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}, label=label)
    return run


def cache_key(data: dict, model: str, call: str) -> str:
    text = (json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n" + model + "\n"
            + PROMPT_REV + "\n" + call)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _call(call: str, data_in: dict, prompt, check, model: str, runner) -> dict:
    """Run one call, or reuse its cached reply. `check(raw)` -> (data, problems). Never raises.
    The envelope is kitplan.plan's, with `data` in place of `plan`."""
    out = {"data": None, "problems": [], "key": cache_key(data_in, model, call), "model": model,
           "cost_usd": 0.0, "spent": 0.0, "cached": False, "reason": "", "unavailable": False}
    key = out["key"]
    cached = kitplan._read_cache(key)
    if isinstance(cached, dict) and cached.get("key") == key and isinstance(cached.get("data"),
                                                                           dict):
        try:
            cost = float(cached.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return {**out, "data": cached["data"], "cached": True, "cost_usd": cost}
    try:
        res = (runner or _default_runner(f"kit {call} (idea planner)"))(prompt(), model)
    except Exception as e:  # noqa: BLE001 - the export reports it, never raises it
        return {**out, "reason": f"model call failed: {type(e).__name__}: {e}"[:300]}
    spent = float(getattr(res, "cost_usd", 0.0) or 0.0)
    out.update(spent=spent, cost_usd=spent)
    if res.failed:
        return {**out, "reason": f"model call failed: {res.error_message()}",
                "unavailable": res.rc == 127}
    raw = schema.extract_json(res.text, dict)
    if raw is None:
        return {**out, "reason": "no JSON object in the model's reply: "
                f"{res.text.strip()[:120]}".strip()}
    data, problems = check(raw)
    if data is None:
        return {**out, "problems": problems, "reason": "; ".join(problems)[:300]}
    kitplan._write_cache(key, {"key": key, "model": model, "cost_usd": spent, "data": data})
    return {**out, "data": data, "problems": problems}


def scope(inp: dict, *, runner=None, model: str = MODEL) -> dict:
    """The scope call: `{data: {requirements, decisions}, problems, key, model, cost_usd, spent,
    cached, reason, unavailable}`; `data` is None when unusable. Cached by input."""
    return _call("scope", inp, lambda: scope_prompt(inp), validate_scope, model, runner)


def _decided_key(decided: list[dict]) -> list[dict]:
    return [{"id": d["id"], "question": d["question"], "answer": d.get("answer")}
            for d in decided]


def plan(inp: dict, scope_data: dict, decided: list[dict], *, runner=None,
         model: str = MODEL) -> dict:
    """The plan call, same envelope; `decided` is `kitplan.resolve_decisions`' list. The cache
    key covers the scope and the answers, so answering a decision re-runs only this call."""
    first = 0 if inp["kind"] == "new" else 1
    data_in = {"input": inp, "scope": scope_data, "decided": _decided_key(decided)}
    return _call("plan", data_in, lambda: plan_prompt(inp, scope_data, decided),
                 lambda raw: validate_plan(raw, first), model, runner)


def lint_specs(stack, spec_texts: dict[int, str]) -> dict[int, list[str]]:
    """Libraries a spec names that the stack does not allow, by step (kitplan.lint's wording)."""
    allowed = kitplan._allowed_libs(stack or [])
    out: dict[int, list[str]] = {}
    if not stack:
        return out
    for n, text in spec_texts.items():
        for lib in kitplan._libraries_in(str(text)):
            if lib in allowed:
                continue
            alt = kitplan._LIBRARIES[lib]
            hint = f" (for example {alt})" if alt else ""
            out.setdefault(n, []).append(
                f"Spec mentions {lib}, which is not in the allowed set: use the standard "
                f"library or the stack{hint} or log it in DEPENDENCIES.md with its reason.")
    return out


# -- the kit files -----------------------------------------------------------------------------
def files(inp: dict, composition, policy: dict, scope_data: dict, plan_data: dict,
          decided: list[dict], *, spec_target: str, target_dir: Path, parts: list | None = None,
          model: str = MODEL, lint: dict | None = None) -> tuple[dict, list, list]:
    """The kit's files keyed by path under .reincarnate/, the `(n, title, spec path)` list and
    the preview rows. CLAUDE.md and run.sh are built from the engine's own pieces, as the parts
    planner builds them; `parts` is `[(name, donor)]` for BLUEPRINT.md's catalog list."""
    from . import kit  # lazily: kit imports this module

    is_new = policy["kind"] == "new"
    stack, test_cmd = policy["stack"], policy["test_cmd"]
    name = kit._name(composition.name, 120)
    allowed = salvage.builder_bash_allowlist(
        policy["ecosystems"] or salvage.manifest_ecosystems(target_dir), test_cmd)
    allowed_text = ", ".join(f"`{a[len('Bash('):-len(':*)')]}`" for a in allowed) or "(none)"
    tests_line = f"Test command: `{test_cmd}`" if test_cmd else "Test command: (none set)"
    if is_new:
        tests_line += " (set in step 0)."
    ui_tools = salvage.ui_test_tools(stack) if is_new else []
    if ui_tools:
        tests_line += "\n\n" + salvage.component_test_line(ui_tools)
    out: dict[str, str] = {}
    out["CLAUDE.md"] = "\n".join([
        f"# Building {name}", "", salvage.BANNER, "", "## Stack", "",
        *([f"- {x}" for x in stack] or ["- (follow the existing project conventions)"]), "",
        "## Dependencies", "", *salvage.dependency_lines(policy), "",
        "## Tests", "", tests_line, "",
        "## Checks", "",
        "After each step the test command runs, then Code Read's turn check "
        "(`cr check-turn`: secrets, tripwires and new dependencies in the step's diff) "
        "when it is installed. A failing check stops the run.", "",
        "Do not edit `.coderead/`, `.reincarnate/state.json`, `.reincarnate/checks/` or the "
        "tick marks in `tasks.md`; run.sh ticks a step once it passes.", "",
        f"Outside a sandbox you may run only these Bash commands: {allowed_text}.", "",
        kitplan.CONTRACTS_LINE, "",
        "## Clean-room rules", "", *salvage.constraint_lines(policy), "",
        "Work one step at a time from .reincarnate/specs/; stop after each step for review.",
        ""])
    out["run.sh"] = (kit.RUN_SH.replace("__TEST_CMD__", kit._shell_quote(test_cmd))
                     .replace("__ALLOWED__", " ".join(kit._shell_quote(a) for a in allowed))
                     .replace("__NEW_TARGET__", "1" if is_new else "0"))
    out["CONTRACTS.md"] = "\n".join([
        f"# Contracts: {name}", "", salvage.BANNER, "", plan_data["contracts_md"], "",
        "## Decisions", "",
        *([f"- {kitplan.decision_line(d)}" for d in decided] or ["(none)"]), ""])

    steps = plan_data["steps"]
    notes = lint if lint is not None else lint_specs(
        stack, {s["n"]: s["spec_md"] for s in steps})
    titles: list[tuple[int, str, str]] = []
    preview: list[dict] = []
    for s in steps:
        n = s["n"]
        slug = kit.slugify(s["title"])[:kit.SLUG_MAX].strip("-") or "step"
        rel = f"specs/{n:02d}-{slug}.md"
        body = re.sub(r"\A# [^\n]*\n+", "", s["spec_md"])  # the model's own title line
        text = f"# Step {n}: {s['title']}\n\n{body}\n"
        if notes.get(n):
            text += ("\n**Contract notes:**\n\n" + "\n".join(f"- {x}" for x in notes[n]) + "\n")
        out[rel] = text
        titles.append((n, s["title"], rel))
        preview.append({
            "n": n, "kind": "scaffold" if is_new and n == 0 else "plan", "title": s["title"],
            "donor": "", "deepened": False, "guard_note": False, "needs": [],
            "checks": kit._check_count(text), "spec_head": "\n".join(text.splitlines()[:40])})

    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    part_lines = [f"- {kit._name(p)} (from {kit._name(d, 200)}): not used by this plan "
                  "(reference notes come later)" for p, d in parts or []]
    out["BLUEPRINT.md"] = "\n".join([
        f"# {name}", "", salvage.BANNER, "",
        f"**Pitch:** {salvage.gate_text(composition.pitch)}", "",
        f"**Idea:** {salvage.gate_text(composition.idea)}", "",
        f"**Target:** {spec_target}", "", f"**Created:** {created}", "",
        f"**Engine:** reincarnate {__version__}", "",
        f"**Planned by:** the idea planner ({model}), from the idea and the stack; catalog "
        "parts are not used to plan", "",
        "## Requirements", "", *([f"- {r}" for r in scope_data["requirements"]] or ["(none)"]),
        "", "## Steps", "", *[f"{n}. Step {n} - {t}" for n, t, _ in titles], "",
        "## Catalog parts", "", *(part_lines or ["(none)"]), ""])
    return out, titles, preview
